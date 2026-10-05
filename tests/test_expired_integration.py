"""Integration tests for the expired-album web workflow.

Covers the dashboard rendering plus the promote / return-to-expired actions,
including the failure paths that used to surface as 500s or clobbered a
running scan.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import spotify_core as core
from app import create_app
from spotify_core import auth as auth_mod
from spotify_core import playlists as playlists_mod
from spotify_core.models import Album, State
from tests.support import ContextTestCase

DAYS_LOOKBACK = 365
# Release-date buckets, relative to now.
ACTIVE = 10
EXPIRED = 400
RETIRED = 900


def _album(album_id, name, days_ago, *, added=False, override=None, uris=None, auto=False):
    return Album(
        id=album_id,
        name=name,
        artist="Test Artist",
        artist_id="artist1",
        album_type="album",
        release_date=(datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
        url=f"https://open.spotify.com/album/{album_id}",
        total_tracks=2,
        first_seen="",
        auto_excluded=auto,
        manual_override=override,
        added_to_playlist=added,
        track_uris=list(uris or []),
    )


class ExpiredWorkflowTests(ContextTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.added = []
        self.removed = []

    # --- helpers ---------------------------------------------------------------

    def _patch(self, target, name, replacement):
        p = patch.object(target, name, replacement)
        p.start()
        self.addCleanup(p.stop)

    def connect(self):
        """Config for the expired workflow plus a connected account.
        Spotify itself is never contacted; tests patch the network calls."""
        self.write_config({
            "days_lookback": DAYS_LOOKBACK,
            "spotify_playlist_id": "playlist123",
        })
        self.write_token("refresh-token")

    def go_offline(self):
        """Patch every outbound Spotify call used by these actions.

        ``core.*`` and ``auth_mod``/``playlists_mod`` are both patched: the routes
        go through the bound ``core`` wrappers, while ``apply_album_override``
        reaches for the submodule functions directly.
        """
        self._patch(core, "get_access_token", lambda *args: "token")
        self._patch(core, "load_refresh_token", lambda: "refresh-token")
        self._patch(core, "get_album_track_uris", lambda album_id, state: ["t:1", "t:2"])
        self._patch(auth_mod, "get_access_token", lambda *args: "token")
        self._patch(auth_mod, "load_refresh_token", lambda *args: "refresh-token")
        self._patch(playlists_mod, "get_album_track_uris",
                    lambda ctx, token, album_id, state: ["t:1", "t:2"])
        self._patch(playlists_mod, "get_playlist_track_uris", lambda *args: [])
        self._patch(playlists_mod, "add_tracks_to_playlist",
                    lambda ctx, token, pid, uris, state: self.added.append((pid, list(uris))))
        self._patch(core, "add_tracks_to_playlist",
                    lambda token, pid, uris, state: self.added.append((pid, list(uris))))
        self._patch(core, "remove_tracks_from_playlist",
                    lambda token, pid, uris, state: self.removed.append((pid, list(uris))))

    def dashboard_html(self):
        self._patch(core, "is_configured", lambda: True)
        self._patch(core, "get_recent_logs", list)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.data.decode()

    # --- Dashboard --------------------------------------------------------------

    def test_dashboard_lists_only_albums_inside_the_expired_window(self):
        self.connect()
        core.save_state(State(known_albums={
            "active": _album("active", "Fresh Release", ACTIVE),
            "expired": _album("expired", "Aged Release", EXPIRED),
            "retired": _album("retired", "Ancient Release", RETIRED),
        }))

        html = self.dashboard_html()

        self.assertIn("Aged Release", html)
        self.assertIn("Fresh Release", html)       # still in the recent-releases table
        self.assertNotIn("Ancient Release", html)  # past retention: not listed anywhere
        self.assertIn("Expired albums (1)", html)

    def test_expired_album_is_not_also_listed_as_excluded(self):
        self.connect()
        core.save_state(State(known_albums={
            "old_live": _album("old_live", "Old Concert (Live)", EXPIRED, auto=True),
            "new_live": _album("new_live", "New Concert (Live)", ACTIVE, auto=True),
        }))

        html = self.dashboard_html()

        self.assertEqual(html.count("Old Concert (Live)"), 1)
        self.assertEqual(html.count("New Concert (Live)"), 1)
        self.assertIn("Expired albums (1)", html)
        self.assertIn("Excluded albums (1)", html)

    def test_promoted_album_stays_in_the_expired_list(self):
        self.connect()
        core.save_state(State(known_albums={
            "p": _album("p", "Promoted Release", EXPIRED, added=True, override=False,
                        uris=["t:1"]),
        }))

        html = self.dashboard_html()

        self.assertIn("Promoted Release", html)
        self.assertIn("Expired albums (1)", html)
        self.assertIn("Remove from playlist", html)

    # --- Promote ----------------------------------------------------------------

    def test_promote_adds_the_album_back_to_the_playlist(self):
        self.connect()
        self.go_offline()
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED),
        }))

        response = self.client.post("/albums/e/promote")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.added, [("playlist123", ["t:1", "t:2"])])
        saved = core.load_state().known_albums["e"]
        self.assertIs(saved.manual_override, False)
        self.assertIs(saved.added_to_playlist, True)

    def test_promote_rejects_albums_outside_the_expired_window(self):
        self.connect()
        self.go_offline()

        for album_id in ("active", "retired", "missing"):
            with self.subTest(album_id=album_id):
                core.save_state(State(known_albums={
                    "active": _album("active", "Fresh Release", ACTIVE),
                    "retired": _album("retired", "Ancient Release", RETIRED),
                }))
                response = self.client.post(f"/albums/{album_id}/promote")
                self.assertEqual(response.status_code, 404)

    def test_promote_requires_a_spotify_connection(self):
        self.write_config({"days_lookback": DAYS_LOOKBACK, "spotify_playlist_id": "playlist123"})
        core.save_state(State(known_albums={"e": _album("e", "Aged Release", EXPIRED)}))
        self._patch(core, "is_connected", lambda: False)

        response = self.client.post("/albums/e/promote")

        self.assertEqual(response.status_code, 400)


    def test_promote_async_returns_immediately_and_reports_progress(self):
        self.connect()
        self.go_offline()
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED),
        }))

        import threading
        started = threading.Event()
        release = threading.Event()

        def slow_apply(album_id, value):
            started.set()
            release.wait(timeout=2)
            return True

        self._patch(playlists_mod, "apply_album_override", slow_apply)
        self._patch(core, "apply_album_override", slow_apply)

        response = self.client.post(
            "/albums/e/promote",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "queued")
        self.assertTrue(started.wait(timeout=2))

        status = self.client.get("/albums/e/promote/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn(status.json["status"], {"running", "completed"})

        release.set()

    def test_multiple_promotes_can_be_queued(self):
        self.connect()
        self.go_offline()
        core.save_state(State(known_albums={
            "e1": _album("e1", "Aged One", EXPIRED),
            "e2": _album("e2", "Aged Two", EXPIRED),
        }))

        import threading
        import time
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_apply(album_id, value):
            calls.append(album_id)
            if album_id == "e1":
                started.set()
                release.wait(timeout=2)
            return True

        self._patch(playlists_mod, "apply_album_override", slow_apply)
        self._patch(core, "apply_album_override", slow_apply)

        first = self.client.post("/albums/e1/promote", headers={"X-Requested-With": "XMLHttpRequest"})
        second = self.client.post("/albums/e2/promote", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertTrue(started.wait(timeout=2))
        self.assertEqual(self.client.get("/albums/e2/promote/status").json["status"], "queued")

        release.set()
        for _ in range(20):
            if len(calls) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(calls, ["e1", "e2"])

    # --- Return to expired ------------------------------------------------------

    def test_return_removes_tracks_and_clears_the_promotion(self):
        self.connect()
        self.go_offline()
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED, added=True, override=False,
                        uris=["t:1", "t:2"]),
        }))

        response = self.client.post("/albums/e/expire")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.removed, [("playlist123", ["t:1", "t:2"])])
        saved = core.load_state().known_albums["e"]
        self.assertIs(saved.added_to_playlist, False)
        self.assertEqual(saved.track_uris, [])
        self.assertIsNone(saved.manual_override)

    def test_return_requires_a_spotify_connection(self):
        """Regression: an unconnected account used to raise AuthError (500) when
        the route called get_access_token unguarded."""
        self.write_config({"days_lookback": DAYS_LOOKBACK, "spotify_playlist_id": "playlist123"})
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
        }))
        self._patch(core, "is_connected", lambda: False)

        def unreachable(*args, **kwargs):
            raise AssertionError("must not reach Spotify without a connection")

        self._patch(core, "get_access_token", unreachable)

        response = self.client.post("/albums/e/expire")

        self.assertEqual(response.status_code, 400)

    def test_return_without_a_playlist_configured_is_rejected(self):
        self.write_config({"days_lookback": DAYS_LOOKBACK, "spotify_playlist_id": ""})
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
        }))
        self._patch(core, "is_connected", lambda: True)

        def unreachable(*args, **kwargs):
            raise AssertionError("must not send a removal without a playlist id")

        self._patch(core, "remove_tracks_from_playlist", unreachable)

        response = self.client.post("/albums/e/expire")

        self.assertEqual(response.status_code, 400)
        saved = core.load_state().known_albums["e"]
        self.assertIs(saved.added_to_playlist, True)

    def test_return_keeps_state_intact_when_the_playlist_call_fails(self):
        self.connect()
        self._patch(core, "is_connected", lambda: True)
        self._patch(core, "load_refresh_token", lambda: "refresh-token")

        def boom(*args, **kwargs):
            raise RuntimeError("spotify is down")

        self._patch(core, "get_access_token", boom)
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
        }))

        response = self.client.post("/albums/e/expire")

        self.assertEqual(response.status_code, 502)
        saved = core.load_state().known_albums["e"]
        self.assertIs(saved.added_to_playlist, True)
        self.assertIs(saved.manual_override, False)

    def test_return_conflicts_while_a_scan_is_running(self):
        self.connect()
        self.go_offline()
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
        }))

        self.assertTrue(core.run_lock.acquire(blocking=False))
        try:
            response = self.client.post("/albums/e/expire")
        finally:
            core.run_lock.release()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.added, [])
        self.assertEqual(self.removed, [])
        self.assertIs(core.load_state().known_albums["e"].added_to_playlist, True)

    def test_return_preserves_other_state_and_releases_the_lock(self):
        """The action must merge its change (not rewrite a stale whole-file
        snapshot) and must not leave run_lock held, which would block every
        later scan."""
        self.connect()
        self.go_offline()
        core.save_state(State(known_albums={
            "e": _album("e", "Aged Release", EXPIRED, added=True, override=False, uris=["t:1"]),
            "other": _album("other", "Fresh Release", ACTIVE, added=True, uris=["t:9"]),
        }))

        response = self.client.post("/albums/e/expire")

        self.assertEqual(response.status_code, 302)
        state = core.load_state()
        self.assertIs(state.known_albums["e"].added_to_playlist, False)
        self.assertIsNone(state.known_albums["e"].manual_override)
        # untouched album keeps its playlist state
        self.assertIs(state.known_albums["other"].added_to_playlist, True)
        self.assertEqual(state.known_albums["other"].track_uris, ["t:9"])
        self.assertFalse(core.run_lock.locked())

    def test_return_rejects_albums_outside_the_expired_window(self):
        self.connect()
        self.go_offline()

        for album_id in ("active", "retired", "missing"):
            with self.subTest(album_id=album_id):
                core.save_state(State(known_albums={
                    "active": _album("active", "Fresh Release", ACTIVE, added=True, uris=["t:1"]),
                    "retired": _album("retired", "Ancient Release", RETIRED, added=True,
                                      uris=["t:1"]),
                }))
                response = self.client.post(f"/albums/{album_id}/expire")
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

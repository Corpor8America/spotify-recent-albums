import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import spotify_core as core
from spotify_core.api import ARTIST_ALBUMS_CATEGORY
from spotify_core.models import Album, Artist, ScanProgress, State
from spotify_core.scan import get_due_artists, record_album
from tests.support import ContextTestCase


def artist_payload(artist_id, name):
    return {"id": artist_id, "name": name}


def album_payload(album_id, name, release_date, artist_id="art1", total_tracks=10):
    return {
        "id": album_id,
        "name": name,
        "album_type": "album",
        "release_date": release_date,
        "total_tracks": total_tracks,
        "artists": [{"id": artist_id}],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


class GetDueArtistsTests(unittest.TestCase):
    def test_all_due_when_no_history(self):
        artists = [artist_payload("a1", "A"), artist_payload("a2", "B")]
        due = get_due_artists(artists, State(), 7)
        self.assertEqual(len(due), 2)

    def test_filters_recently_checked(self):
        artists = [artist_payload("a1", "A"), artist_payload("a2", "B")]
        state = State(artists={
            "a1": Artist(id="a1", name="A", last_checked=datetime.now(timezone.utc).isoformat()),
        })
        due = get_due_artists(artists, state, 7)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], "a2")

    def test_selects_oldest_batch_when_none_are_overdue(self):
        now = datetime.now(timezone.utc)
        artists = [artist_payload("a1", "A"), artist_payload("a2", "B"),
                   artist_payload("a3", "C"), artist_payload("a4", "D")]
        state = State(artists={
            "a1": Artist(id="a1", name="A", last_checked=(now - timedelta(days=1)).isoformat()),
            "a2": Artist(id="a2", name="B", last_checked=(now - timedelta(days=2)).isoformat()),
            "a3": Artist(id="a3", name="C", last_checked=(now - timedelta(hours=6)).isoformat()),
            "a4": Artist(id="a4", name="D", last_checked=(now - timedelta(hours=12)).isoformat()),
        })
        due = get_due_artists(artists, state, 3)
        self.assertEqual([a["id"] for a in due], ["a2"])


class RecordAlbumTests(unittest.TestCase):
    def test_preserves_manual_override(self):
        state = State(known_albums={
            "a1": Album(id="a1", name="old", artist="Artist", artist_id="art1",
                        album_type="album", release_date="2026-01-01", url="", total_tracks=5,
                        first_seen="", manual_override=True),
        })
        record_album(state, artist_payload("art1", "Artist"),
                     album_payload("a1", "Live (Live)", "2026-01-01"),
                     "2026-07-01T00:00:00+00:00")
        entry = state.known_albums["a1"]
        self.assertTrue(entry.manual_override)
        self.assertTrue(entry.auto_excluded)

    def test_new_album_defaults(self):
        state = State()
        record_album(state, artist_payload("art1", "Artist"),
                     album_payload("a2", "Studio Album", "2026-01-01"),
                     "2026-07-01T00:00:00+00:00")
        entry = state.known_albums["a2"]
        self.assertFalse(entry.auto_excluded)
        self.assertIsNone(entry.manual_override)
        self.assertEqual(entry.first_seen, "2026-07-01T00:00:00+00:00")


class StartScanThreadTests(ContextTestCase):
    """Regression: the background thread must receive the context. The
    Docker integration run caught run_scan() being spawned without it,
    which crashed the thread instantly and left scan_running stuck true."""

    def test_passes_context_and_options_to_background_scan(self):
        done = threading.Event()
        seen = {}

        def fake_run_scan(ctx, **kwargs):
            seen["ctx"] = ctx
            seen.update(kwargs)
            core.scan.run_lock.release()  # lock_held=True contract
            done.set()

        with patch.object(core.scan, "run_scan", side_effect=fake_run_scan):
            self.assertTrue(core.start_scan(self.ctx))
            self.assertTrue(done.wait(5), "background scan never ran")

        self.assertIs(seen["ctx"], self.ctx)
        self.assertTrue(seen["lock_held"])

    def test_second_start_while_running_returns_false(self):
        # run_lock is acquired synchronously by start_scan, so the second
        # call must refuse even if the background thread has not started.
        release = threading.Event()

        def fake_run_scan(ctx, **kwargs):
            release.wait(5)
            core.scan.run_lock.release()

        with patch.object(core.scan, "run_scan", side_effect=fake_run_scan):
            self.assertTrue(core.start_scan(self.ctx))
            self.assertFalse(core.start_scan(self.ctx))
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and core.scan.run_lock.locked():
            time.sleep(0.05)
        self.assertFalse(core.scan.run_lock.locked())

    def test_crashing_thread_clears_in_progress_marker(self):
        core.save_state(State(in_progress=ScanProgress(due_ids=["a1"], processed_ids=[])))

        def exploding_run_scan(ctx, **kwargs):
            core.scan.run_lock.release()
            raise RuntimeError("boom")

        with patch.object(core.scan, "run_scan", side_effect=exploding_run_scan):
            self.assertTrue(core.start_scan(self.ctx))
            deadline = time.time() + 5
            while time.time() < deadline:
                if core.load_state().in_progress is None:
                    break
                time.sleep(0.05)
        self.assertIsNone(core.load_state().in_progress)


class RunScanBlockedCategoryTests(ContextTestCase):
    """A scan must not start a phase whose endpoint category is already
    rate-limited: it should skip the phase, keep the persisted block, and
    leave resume progress untouched."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def _seed_resume_state(self, blocked_until_ts):
        artists = [artist_payload(f"a{i}", f"Artist {i}") for i in range(10)]
        core.save_state(State(
            in_progress=ScanProgress(
                due_ids=[a["id"] for a in artists],
                processed_ids=[a["id"] for a in artists[:5]],
            ),
            rate_limits={ARTIST_ALBUMS_CATEGORY: blocked_until_ts},
        ))
        return artists

    def test_skips_album_phase_when_album_category_blocked(self):
        artists = self._seed_resume_state(int(time.time()) + 3600)
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums",
                          side_effect=AssertionError("album phase must be skipped")) as albums:
            result = core.run_scan(days=365, interval_days=3, min_request_interval=0)

        self.assertEqual(result["blocked_categories"], [ARTIST_ALBUMS_CATEGORY])
        albums.assert_not_called()
        state = core.load_state()
        self.assertIn(ARTIST_ALBUMS_CATEGORY, state.rate_limits)
        self.assertEqual(len(state.in_progress.processed_ids), 5)

    def test_runs_album_phase_when_not_blocked(self):
        artists = [artist_payload(f"a{i}", f"Artist {i}") for i in range(2)]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as albums:
            result = core.run_scan(days=365, interval_days=3, min_request_interval=0)

        self.assertEqual(result["blocked_categories"], [])
        self.assertEqual(albums.call_count, 2)
        state = core.load_state()
        self.assertEqual(state.rate_limits, {})
        self.assertIsNone(state.in_progress)


class RunScanResumeTests(ContextTestCase):
    """An interrupted scan resumes from where it left off."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def test_resume_only_processes_remaining_artists(self):
        artists = [artist_payload("a1", "A"), artist_payload("a2", "B"),
                   artist_payload("a3", "C")]
        core.save_state(State(in_progress=ScanProgress(
            due_ids=["a1", "a2", "a3"], processed_ids=["a1"])))
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as albums:
            result = core.run_scan(days=365, interval_days=3)

        self.assertEqual(result["status"], "ok")
        self.assertEqual([call.args[2] for call in albums.call_args_list], ["a2", "a3"])
        state = core.load_state()
        self.assertIsNone(state.in_progress)
        self.assertEqual(set(state.artists.keys()), {"a2", "a3"})

    def test_unreleased_album_not_added_to_playlist(self):
        future = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        artists = [artist_payload("art1", "Artist")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums",
                          return_value=[album_payload("alb1", "Future Album", future)]), \
             patch.object(core.scan, "add_tracks_to_playlist") as add, \
             patch.object(core.scan, "get_album_track_uris") as fetch:
            self.write_config({"spotify_playlist_id": "playlist123"})
            result = core.run_scan(days=365, interval_days=3)

        self.assertEqual(result["status"], "ok")
        add.assert_not_called()
        fetch.assert_not_called()
        entry = core.load_state().known_albums["alb1"]
        self.assertFalse(entry.added_to_playlist)
        self.assertEqual(entry.track_uris, [])

    def test_recent_album_added_to_playlist(self):
        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        artists = [artist_payload("art1", "Artist")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums",
                          return_value=[album_payload("alb1", "New Album", recent)]), \
             patch.object(core.scan, "get_album_track_uris",
                          return_value=["spotify:track:x"]) as fetch, \
             patch.object(core.scan, "add_tracks_to_playlist") as add:
            self.write_config({"spotify_playlist_id": "playlist123"})
            result = core.run_scan(days=365, interval_days=3)

        self.assertEqual(result["status"], "ok")
        fetch.assert_called_once()
        add.assert_called_once()
        # add_tracks_to_playlist(ctx, token, playlist_id, track_uris, state)
        self.assertEqual(add.call_args.args[3], ["spotify:track:x"])
        entry = core.load_state().known_albums["alb1"]
        self.assertTrue(entry.added_to_playlist)
        self.assertEqual(entry.track_uris, ["spotify:track:x"])


class RunScanNotConnectedTests(ContextTestCase):
    def setUp(self):
        super().setUp()

    def test_returns_not_connected_when_no_token(self):
        result = core.run_scan(days=365, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "not_connected")

    def test_returns_not_connected_when_no_creds(self):
        self.write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        self.write_token("some-token")
        result = core.run_scan(days=365, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "not_connected")


if __name__ == "__main__":
    unittest.main()

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import spotify_core as core
from spotify_core.api import ARTIST_ALBUMS_CATEGORY
from spotify_core.models import Album, Artist, ScanProgress, State
from spotify_core.scan import get_due_artists, record_album, _plan_artists, _build_priority_order, _maybe_auto_disable_priority_scan, _maybe_auto_reorder
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
             patch.object(core.scan, "get_album_track_uris") as fetch, \
             patch.object(core.scan, "_maybe_auto_reorder"):
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
             patch.object(core.scan, "add_tracks_to_playlist") as add, \
             patch.object(core.scan, "_maybe_auto_reorder"):
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


class PlanArtistsTests(ContextTestCase):
    """Tests for _plan_artists wiring with priority support."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def _artists(self):
        return [artist_payload("a1", "A"), artist_payload("a2", "B"),
                artist_payload("a3", "C")]

    def test_use_priority_false_returns_three_values(self):
        artists = self._artists()
        result = _plan_artists(self.ctx, State(), artists, 7, [], 365, use_priority=False)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)
        due, processed, used_priority = result
        self.assertFalse(used_priority)

    def test_use_priority_false_order_unchanged(self):
        artists = self._artists()
        state = State()
        due, _, _ = _plan_artists(self.ctx, state, artists, 7, [], 365, use_priority=False)
        due_ids = [a["id"] for a in due]
        # Should be same as get_due_artists output
        expected = [a["id"] for a in get_due_artists(artists, state, 7)]
        self.assertEqual(due_ids, expected)

    @patch("spotify_core.scan._build_priority_order", return_value=["a2", "a1"])
    def test_use_priority_true_reorders(self, mock_build):
        artists = self._artists()
        due, _, used_priority = _plan_artists(
            self.ctx, State(), artists, 7, [], 365, use_priority=True)
        self.assertTrue(used_priority)
        due_ids = [a["id"] for a in due]
        self.assertEqual(due_ids[0], "a2")
        self.assertEqual(due_ids[1], "a1")

    @patch("spotify_core.scan._build_priority_order", return_value=[])
    def test_use_priority_true_zero_hits_same_order(self, mock_build):
        artists = self._artists()
        state = State()
        due_default, _, _ = _plan_artists(self.ctx, State(), artists, 7, [], 365, use_priority=False)
        due_priority, _, used_priority = _plan_artists(self.ctx, state, artists, 7, [], 365, use_priority=True)
        self.assertTrue(used_priority)
        # Zero MB hits -> same order as get_due_artists
        self.assertEqual([a["id"] for a in due_priority], [a["id"] for a in due_default])

    def test_blocked_category_returns_none(self):
        artists = self._artists()
        state = State(rate_limits={ARTIST_ALBUMS_CATEGORY: int(time.time()) + 3600})
        result = _plan_artists(self.ctx, state, artists, 7, [], 365, use_priority=True)
        self.assertIsNone(result)

    def test_resume_preserves_processed_ids(self):
        artists = self._artists()
        state = State(in_progress=ScanProgress(
            due_ids=["a1", "a2", "a3"], processed_ids=["a1"]))
        result = _plan_artists(self.ctx, state, artists, 7, [], 365, use_priority=False)
        self.assertIsNotNone(result)
        due, processed, _ = result
        self.assertIn("a1", processed)

    @patch("spotify_core.scan._build_priority_order", return_value=["a3"])
    def test_resume_priority_reorders_remaining(self, mock_build):
        artists = self._artists()
        state = State(in_progress=ScanProgress(
            due_ids=["a1", "a2", "a3"], processed_ids=["a1"]))
        due, processed, used_priority = _plan_artists(
            self.ctx, state, artists, 7, [], 365, use_priority=True)
        self.assertTrue(used_priority)
        due_ids = [a["id"] for a in due]
        # a1 is processed, should be first
        self.assertEqual(due_ids[0], "a1")
        # a3 is priority, should come next
        self.assertEqual(due_ids[1], "a3")
        # a2 is non-priority remaining
        self.assertEqual(due_ids[2], "a2")

    def test_use_priority_false_does_not_call_build(self):
        artists = self._artists()
        with patch("spotify_core.scan._build_priority_order") as mock_build:
            _plan_artists(self.ctx, State(), artists, 7, [], 365, use_priority=False)
            mock_build.assert_not_called()

    def test_blocked_category_preserves_setting(self):
        """When album scanning is blocked, _plan_artists returns None but
        does not change the priority setting."""
        artists = self._artists()
        state = State(rate_limits={ARTIST_ALBUMS_CATEGORY: int(time.time()) + 3600})
        self.write_config({"musicbrainz_priority_scan": True})
        result = _plan_artists(self.ctx, state, artists, 7, [], 365, use_priority=True)
        self.assertIsNone(result)
        cfg = core.load_config()
        self.assertTrue(cfg["musicbrainz_priority_scan"])


class AutoDisablePriorityScanTests(ContextTestCase):
    """Tests for _maybe_auto_disable_priority_scan."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def test_disables_after_clean_completion(self):
        cfg = {"musicbrainz_priority_scan": True}
        state = State()
        _maybe_auto_disable_priority_scan(self.ctx, cfg, True, [], state)
        self.assertFalse(cfg["musicbrainz_priority_scan"])

    def test_does_not_disable_when_priority_not_used(self):
        cfg = {"musicbrainz_priority_scan": True}
        state = State()
        _maybe_auto_disable_priority_scan(self.ctx, cfg, False, [], state)
        self.assertTrue(cfg["musicbrainz_priority_scan"])

    def test_does_not_disable_when_blocked(self):
        cfg = {"musicbrainz_priority_scan": True}
        state = State()
        _maybe_auto_disable_priority_scan(self.ctx, cfg, True, [ARTIST_ALBUMS_CATEGORY], state)
        self.assertTrue(cfg["musicbrainz_priority_scan"])

    def test_does_not_disable_when_in_progress(self):
        cfg = {"musicbrainz_priority_scan": True}
        state = State(in_progress=ScanProgress(due_ids=["a1"], processed_ids=[]))
        _maybe_auto_disable_priority_scan(self.ctx, cfg, True, [], state)
        self.assertTrue(cfg["musicbrainz_priority_scan"])

    def test_no_config_write_when_already_false(self):
        cfg = {"musicbrainz_priority_scan": False}
        state = State()
        with patch.object(core.config, "save_config") as mock_save:
            _maybe_auto_disable_priority_scan(self.ctx, cfg, True, [], state)
            mock_save.assert_not_called()

    def test_persists_config(self):
        self.write_config({"musicbrainz_priority_scan": True})
        state = State()
        _maybe_auto_disable_priority_scan(self.ctx, self.config, True, [], state)
        loaded = core.load_config()
        self.assertFalse(loaded["musicbrainz_priority_scan"])


class AutoReorderTests(ContextTestCase):
    """Tests for _maybe_auto_reorder."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def test_no_new_albums_skips_check(self):
        state = State()
        with patch("spotify_core.scan.playlist_order_is_stale") as mock_stale:
            _maybe_auto_reorder(self.ctx, "token", state, "playlist", [], False)
            mock_stale.assert_not_called()

    def test_no_playlist_skips(self):
        state = State()
        with patch("spotify_core.scan.playlist_order_is_stale") as mock_stale:
            _maybe_auto_reorder(self.ctx, "token", state, None, [], True)
            mock_stale.assert_not_called()

    def test_blocked_categories_skips(self):
        state = State()
        with patch("spotify_core.scan.playlist_order_is_stale") as mock_stale:
            _maybe_auto_reorder(self.ctx, "token", state, "playlist",
                                [ARTIST_ALBUMS_CATEGORY], True)
            mock_stale.assert_not_called()

    @patch("spotify_core.scan.reorder_playlist")
    @patch("spotify_core.scan.playlist_order_is_stale", return_value=True)
    def test_stale_triggers_reorder(self, mock_stale, mock_reorder):
        state = State()
        _maybe_auto_reorder(self.ctx, "token", state, "playlist", [], True)
        mock_reorder.assert_called_once()

    @patch("spotify_core.scan.reorder_playlist")
    @patch("spotify_core.scan.playlist_order_is_stale", return_value=False)
    def test_not_stale_no_reorder(self, mock_stale, mock_reorder):
        state = State()
        _maybe_auto_reorder(self.ctx, "token", state, "playlist", [], True)
        mock_reorder.assert_not_called()

    @patch("spotify_core.scan.reorder_playlist")
    @patch("spotify_core.scan.playlist_order_is_stale",
           side_effect=core.errors.RateLimitError("token", 1))
    def test_rate_limit_on_staleness_check_caught(self, mock_stale, mock_reorder):
        state = State()
        # Should not raise
        _maybe_auto_reorder(self.ctx, "token", state, "playlist", [], True)
        mock_reorder.assert_not_called()


class RunScanWiringTests(ContextTestCase):
    """Tests for run_scan wiring with priority and auto-disable."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def test_plan_artists_receives_days_and_priority_config(self):
        artists = [artist_payload("a1", "A")]
        self.write_config({"musicbrainz_priority_scan": True})
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "_plan_artists") as mock_plan, \
             patch.object(core.scan, "_maybe_auto_disable_priority_scan") as mock_disable:
            mock_plan.return_value = ([], set(), False)
            core.run_scan(days=180, interval_days=3, min_request_interval=0)
            mock_plan.assert_called_once()
            args, kwargs = mock_plan.call_args
            # args: (ctx, state, artists, interval_days, blocked_categories, days, use_priority)
            self.assertEqual(args[5], 180)  # days_lookback
            self.assertTrue(args[6])  # use_priority

    def test_process_artists_receives_two_item_plan(self):
        artists = [artist_payload("a1", "A")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "_plan_artists") as mock_plan, \
             patch.object(core.scan, "_process_artists") as mock_process, \
             patch.object(core.scan, "_maybe_auto_disable_priority_scan"):
            mock_plan.return_value = ([artist_payload("a1", "A")], set(), False)
            mock_process.return_value = False
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
            args, kwargs = mock_process.call_args
            plan_arg = args[3]  # fourth positional arg is the plan tuple
            self.assertEqual(len(plan_arg), 2)

    def test_auto_disable_called_after_finalize(self):
        artists = [artist_payload("a1", "A")]
        call_order = []
        original_finalize = core.scan._finalize_progress
        original_disable = core.scan._maybe_auto_disable_priority_scan

        def track_finalize(ctx, state, blocked):
            call_order.append("finalize")
            original_finalize(ctx, state, blocked)

        def track_disable(ctx, cfg, used, blocked, state):
            call_order.append("disable")
            original_disable(ctx, cfg, used, blocked, state)

        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]), \
             patch.object(core.scan, "_finalize_progress", side_effect=track_finalize), \
             patch.object(core.scan, "_maybe_auto_disable_priority_scan", side_effect=track_disable), \
             patch("spotify_core.scan.get_albums_in_window", return_value=[]), \
             patch("spotify_core.scan.get_artist_active", return_value=True), \
             patch("spotify_core.scan.resolve_spotify_to_mb", return_value=None):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
        self.assertEqual(call_order, ["finalize", "disable"])

    def test_priority_scan_enabled_passed_to_plan(self):
        artists = [artist_payload("a1", "A")]
        self.write_config({"musicbrainz_priority_scan": True})
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "_plan_artists") as mock_plan, \
             patch.object(core.scan, "_maybe_auto_disable_priority_scan"):
            mock_plan.return_value = ([], set(), False)
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
            args, kwargs = mock_plan.call_args
            self.assertTrue(args[6])  # use_priority

    def test_priority_scan_disabled_by_default(self):
        artists = [artist_payload("a1", "A")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "_plan_artists") as mock_plan, \
             patch.object(core.scan, "_maybe_auto_disable_priority_scan"):
            mock_plan.return_value = ([], set(), False)
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
            args, kwargs = mock_plan.call_args
            self.assertFalse(args[6])  # use_priority


if __name__ == "__main__":
    unittest.main()

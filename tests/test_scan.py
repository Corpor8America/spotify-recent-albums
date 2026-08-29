import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import spotify_core as core
from spotify_core.api import ARTIST_ALBUMS_CATEGORY
from spotify_core.models import Album, Artist, ScanProgress, State
from spotify_core.scan import get_due_artists, record_album, _plan_artists, _mb_classify_and_order, _active_check_is_fresh, _maybe_auto_reorder
from tests.support import ContextTestCase


def artist_payload(artist_id, name):
    return {"id": artist_id, "name": name}


def _classify_passthrough(ctx, state, artists, days_lookback, total_count, interval_days):
    """Classifier fake that keeps every artist in their original order."""
    return [a["id"] for a in artists], set()


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

    @patch("spotify_core.scan._mb_classify_and_order", side_effect=_classify_passthrough)
    def test_runs_album_phase_when_not_blocked(self, mock_classify):
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

    @patch("spotify_core.scan._mb_classify_and_order", side_effect=_classify_passthrough)
    def test_resume_only_processes_remaining_artists(self, mock_classify):
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

    @patch("spotify_core.scan._mb_classify_and_order", side_effect=_classify_passthrough)
    def test_unreleased_album_not_added_to_playlist(self, mock_classify):
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

    @patch("spotify_core.scan._mb_classify_and_order", side_effect=_classify_passthrough)
    def test_recent_album_added_to_playlist(self, mock_classify):
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
    """Tests for _plan_artists wiring around the MB classifier."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def _artists(self):
        return [artist_payload("a1", "A"), artist_payload("a2", "B"),
                artist_payload("a3", "C")]

    @patch("spotify_core.scan._mb_classify_and_order",
           return_value=(["a1", "a2", "a3"], set()))
    def test_returns_three_values(self, mock_classify):
        artists = self._artists()
        result = _plan_artists(self.ctx, State(), artists, 7, [], 365)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)

    @patch("spotify_core.scan._mb_classify_and_order",
           return_value=(["a1", "a2", "a3"], set()))
    def test_order_unchanged_when_no_hits(self, mock_classify):
        artists = self._artists()
        state = State()
        due, _, _ = _plan_artists(self.ctx, state, artists, 7, [], 365)
        due_ids = [a["id"] for a in due]
        expected = [a["id"] for a in get_due_artists(artists, state, 7)]
        self.assertEqual(due_ids, expected)
        mock_classify.assert_called_once()

    @patch("spotify_core.scan._mb_classify_and_order",
           return_value=(["a2", "a1", "a3"], set()))
    def test_hits_reorder(self, mock_classify):
        artists = self._artists()
        due, _, _ = _plan_artists(self.ctx, State(), artists, 7, [], 365)
        due_ids = [a["id"] for a in due]
        self.assertEqual(due_ids[0], "a2")
        self.assertEqual(due_ids[1], "a1")
        self.assertEqual(due_ids[2], "a3")

    @patch("spotify_core.scan._mb_classify_and_order")
    def test_blocked_category_returns_none(self, mock_classify):
        artists = self._artists()
        state = State(rate_limits={ARTIST_ALBUMS_CATEGORY: int(time.time()) + 3600})
        result = _plan_artists(self.ctx, state, artists, 7, [], 365)
        self.assertIsNone(result)
        mock_classify.assert_not_called()

    @patch("spotify_core.scan._mb_classify_and_order",
           return_value=(["a2", "a3"], set()))
    def test_resume_preserves_processed_ids(self, mock_classify):
        artists = self._artists()
        state = State(in_progress=ScanProgress(
            due_ids=["a1", "a2", "a3"], processed_ids=["a1"]))
        result = _plan_artists(self.ctx, state, artists, 7, [], 365)
        self.assertIsNotNone(result)
        due, processed, _ = result
        self.assertIn("a1", processed)
        # a1 was already processed and stays first
        due_ids = [a["id"] for a in due]
        self.assertEqual(due_ids[0], "a1")

    @patch("spotify_core.scan._mb_classify_and_order",
           return_value=(["a3", "a2"], set()))
    def test_resume_reorders_remaining(self, mock_classify):
        artists = self._artists()
        state = State(in_progress=ScanProgress(
            due_ids=["a1", "a2", "a3"], processed_ids=["a1"]))
        due, _, _ = _plan_artists(self.ctx, state, artists, 7, [], 365)
        due_ids = [a["id"] for a in due]
        self.assertEqual(due_ids[0], "a1")
        self.assertEqual(due_ids[1], "a3")
        self.assertEqual(due_ids[2], "a2")

    @patch("spotify_core.scan._mb_classify_and_order",
           return_value=(["a2", "a1", "a3"], {"a1"}))
    def test_skip_ids_propagated(self, mock_classify):
        artists = self._artists()
        due, _, skip_ids = _plan_artists(self.ctx, State(), artists, 7, [], 365)
        self.assertEqual(skip_ids, {"a1"})

    def test_persists_resolved_state_from_classifier(self):
        artists = self._artists()
        state = State()

        def classify(ctx, state, artists, days_lookback, total_count, interval_days):
            state.artists["a1"] = Artist(id="a1", name="A", musicbrainz_id="mb-a1",
                                         mb_active=True)
            return [a["id"] for a in artists], set()

        with patch("spotify_core.scan._mb_classify_and_order", side_effect=classify):
            _plan_artists(self.ctx, state, artists, 7, [], 365)
        self.assertEqual(state.artists["a1"].musicbrainz_id, "mb-a1")


class MbActiveCheckFreshTests(unittest.TestCase):
    """Tests for _active_check_is_fresh (30-day inactive re-check gate)."""

    def test_fresh_check_returns_false_for_inactive_only(self):
        entry = Artist(id="a1", name="A", mb_active=False,
                       mb_active_checked=datetime.now(timezone.utc).isoformat())
        # The gate only matters for inactive artists, but the helper just
        # answers whether the stored timestamp is fresh.
        self.assertTrue(_active_check_is_fresh(entry))

    def test_stale_check_returns_false(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        entry = Artist(id="a1", name="A", mb_active=False, mb_active_checked=stale)
        self.assertFalse(_active_check_is_fresh(entry))

    def test_empty_checked_returns_false(self):
        entry = Artist(id="a1", name="A", mb_active=False, mb_active_checked="")
        self.assertFalse(_active_check_is_fresh(entry))

    def test_garbage_checked_returns_false(self):
        entry = Artist(id="a1", name="A", mb_active=False, mb_active_checked="nope")
        self.assertFalse(_active_check_is_fresh(entry))


class MbClassifyAndOrderTests(unittest.TestCase):
    """Tests for _mb_classify_and_order (the MusicBrainz pre-pass)."""

    def _artist(self, artist_id, name):
        return {"id": artist_id, "name": name}

    @patch("spotify_core.scan.get_artist_status_and_release_groups",
           return_value=(True, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_active_no_releases_is_normal_not_skipped(self, mock_resolve, mock_status):
        artists = [self._artist("a1", "A")]
        ordered, skip = _mb_classify_and_order(MagicMock(), State(), artists, 365, 10, 7)
        self.assertEqual(ordered, ["a1"])
        self.assertEqual(skip, set())

    @patch("spotify_core.scan.get_artist_status_and_release_groups",
           return_value=(False, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_inactive_is_skipped_and_cached(self, mock_resolve, mock_status):
        artists = [self._artist("a1", "A")]
        state = State()
        ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        self.assertEqual(skip, {"a1"})
        entry = state.artists["a1"]
        self.assertFalse(entry.mb_active)
        self.assertNotEqual(entry.mb_active_checked, "")

    def test_fresh_inactive_skip_makes_no_mb_call(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        state = State(artists={
            "a1": Artist(id="a1", name="A", musicbrainz_id="mb-123",
                         mb_active=False, mb_active_checked=now_iso),
        })
        artists = [self._artist("a1", "A")]
        with patch("spotify_core.scan.get_artist_status_and_release_groups") as mock_status, \
             patch("spotify_core.scan.resolve_spotify_to_mb") as mock_resolve:
            ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        mock_status.assert_not_called()
        mock_resolve.assert_not_called()
        self.assertEqual(skip, {"a1"})

    def test_stale_inactive_rechecks_and_stays_skipped(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        state = State(artists={
            "a1": Artist(id="a1", name="A", musicbrainz_id="mb-123",
                         mb_active=False, mb_active_checked=stale),
        })
        artists = [self._artist("a1", "A")]
        with patch("spotify_core.scan.get_artist_status_and_release_groups",
                   return_value=(False, [])) as mock_status, \
             patch("spotify_core.scan.resolve_spotify_to_mb") as mock_resolve:
            ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        mock_status.assert_called_once()
        mock_resolve.assert_not_called()
        self.assertEqual(skip, {"a1"})
        self.assertFalse(state.artists["a1"].mb_active)

    def test_stale_inactive_recheck_flips_to_active(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        state = State(artists={
            "a1": Artist(id="a1", name="A", musicbrainz_id="mb-123",
                         mb_active=False, mb_active_checked=stale),
        })
        artists = [self._artist("a1", "A")]
        with patch("spotify_core.scan.get_artist_status_and_release_groups",
                   return_value=(True, [{
                       "id": "rg1", "primary-type": "Album",
                       "first-release-date": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"),
                   }])) as mock_status, \
             patch("spotify_core.scan.resolve_spotify_to_mb"):
            ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        self.assertEqual(ordered, ["a1"])
        self.assertTrue(state.artists["a1"].mb_active)

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_hits_first_oldest_release_first(self, mock_resolve, mock_status):
        ten_days_ago = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        def status_side_effect(ctx, mbid):
            if mbid == "mb-a1":
                return True, [{"id": "r1", "primary-type": "Album",
                               "first-release-date": ten_days_ago}]
            if mbid == "mb-a2":
                return True, [{"id": "r2", "primary-type": "Album",
                               "first-release-date": thirty_days_ago}]
            return True, []

        def resolve_side_effect(spotify_id):
            return "mb-" + spotify_id

        mock_resolve.side_effect = resolve_side_effect
        mock_status.side_effect = status_side_effect
        artists = [self._artist("a1", "A"), self._artist("a2", "B"), self._artist("a3", "C")]
        ordered, skip = _mb_classify_and_order(MagicMock(), State(), artists, 365, 30, 7)
        # a2 released earlier, so it sorts before a1; hits lead the list
        self.assertEqual(ordered[:2], ["a2", "a1"])
        self.assertEqual(skip, set())

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_future_only_skipped_and_recorded(self, mock_resolve, mock_status):
        mock_resolve.return_value = "mb-123"
        mock_status.return_value = (True, [{
            "id": "rg-future", "primary-type": "Album",
            "first-release-date": "2099-12-31",
        }])
        artists = [self._artist("a1", "A")]
        state = State()
        ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        self.assertEqual(skip, {"a1"})
        # Skipped artists stay in the ordering so the Spotify loop can mark
        # them done, but they are recorded as future-only.
        self.assertIn("rg-future", state.musicbrainz_upcoming)
        self.assertEqual(state.musicbrainz_upcoming["rg-future"].artist_id, "a1")

    def test_future_only_artist_last_checked_not_updated(self):
        """Skipped artists stay due: the classifier must not touch last_checked."""
        now_iso = datetime.now(timezone.utc).isoformat()
        state = State(artists={
            "a1": Artist(id="a1", name="A", musicbrainz_id="mb-123",
                         last_checked=now_iso, mb_active=True,
                         mb_active_checked=now_iso),
        })
        artists = [self._artist("a1", "A")]
        with patch("spotify_core.scan.get_artist_status_and_release_groups",
                   return_value=(True, [{
                       "id": "rg-f", "primary-type": "Album",
                       "first-release-date": "2099-12-31",
                   }])), \
             patch("spotify_core.scan.resolve_spotify_to_mb"):
            _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        self.assertEqual(state.artists["a1"].last_checked, now_iso)

    def test_unresolvable_artist_is_normal(self):
        artists = [self._artist("a1", "A")]
        with patch("spotify_core.scan.resolve_spotify_to_mb", return_value=None), \
             patch("spotify_core.scan.get_artist_status_and_release_groups") as mock_status:
            ordered, skip = _mb_classify_and_order(MagicMock(), State(), artists, 365, 10, 7)
        mock_status.assert_not_called()
        self.assertEqual(ordered, ["a1"])
        self.assertEqual(skip, set())

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_early_stop_once_batch_worth_hits(self, mock_resolve, mock_status):
        # total_count=30, interval_days=7 -> batch_worth=4. A 10-artist due
        # batch stops querying after 4 hits; the rest stay in the tail.
        def resolve_side_effect(spotify_id):
            return "mb-" + spotify_id[1:]  # "a1" -> "mb-1"

        def status_side_effect(ctx, mbid):
            k = int(mbid.rsplit("-", 1)[1])
            date = (datetime.now() - timedelta(days=k)).strftime("%Y-%m-%d")
            return True, [{"id": f"rg-{mbid}", "primary-type": "Album",
                           "first-release-date": date}]

        mock_resolve.side_effect = resolve_side_effect
        mock_status.side_effect = status_side_effect
        artists = [self._artist(f"a{i}", f"A{i}") for i in range(1, 11)]
        ordered, skip = _mb_classify_and_order(MagicMock(), State(), artists, 365, 30, 7)
        self.assertEqual(mock_status.call_count, 4)
        self.assertEqual(len(ordered), 10)  # tail preserved in original order
        self.assertEqual(skip, set())

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_hit_also_records_future_albums(self, mock_resolve, mock_status):
        mock_resolve.return_value = "mb-123"
        mock_status.return_value = (True, [
            {"id": "rg-recent", "primary-type": "Album",
             "first-release-date": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")},
            {"id": "rg-future", "primary-type": "Album",
             "first-release-date": "2099-06-01"},
        ])
        artists = [self._artist("a1", "A")]
        state = State()
        ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        self.assertEqual(ordered, ["a1"])
        self.assertEqual(skip, set())
        self.assertIn("rg-future", state.musicbrainz_upcoming)

    def test_lookup_failure_falls_back_to_normal(self):
        artists = [self._artist("a1", "A")]
        state = State(artists={"a1": Artist(id="a1", name="A", musicbrainz_id="mb-123")})
        with patch("spotify_core.scan.get_artist_status_and_release_groups",
                   side_effect=RuntimeError("MB down")), \
             patch("spotify_core.scan.resolve_spotify_to_mb"):
            ordered, skip = _mb_classify_and_order(MagicMock(), state, artists, 365, 10, 7)
        self.assertEqual(ordered, ["a1"])
        self.assertEqual(skip, set())


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
    """Tests for run_scan wiring with the unconditional MB classifier."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def test_plan_artists_receives_days_and_total_count(self):
        artists = [artist_payload("a1", "A")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "_plan_artists") as mock_plan:
            mock_plan.return_value = ([], set(), set())
            core.run_scan(days=180, interval_days=3, min_request_interval=0)
            mock_plan.assert_called_once()
            args, kwargs = mock_plan.call_args
            # args: (ctx, state, artists, interval_days, blocked_categories, days)
            self.assertEqual(args[5], 180)  # days_lookback
            self.assertEqual(kwargs.get("total_artist_count"), 1)  # len(artists)

    def test_process_artists_receives_three_item_plan(self):
        artists = [artist_payload("a1", "A")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "_plan_artists") as mock_plan, \
             patch.object(core.scan, "_process_artists") as mock_process:
            mock_plan.return_value = ([artist_payload("a1", "A")], set(), {"a1"})
            mock_process.return_value = False
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
            args, kwargs = mock_process.call_args
            plan_arg = args[3]  # fourth positional arg is the plan tuple
            self.assertEqual(len(plan_arg), 3)
            self.assertEqual(plan_arg[2], {"a1"})

    def test_finalize_runs_after_process(self):
        artists = [artist_payload("a1", "A")]
        call_order = []
        original_finalize = core.scan._finalize_progress

        def track_finalize(ctx, state, blocked):
            call_order.append("finalize")
            original_finalize(ctx, state, blocked)

        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]), \
             patch.object(core.scan, "_finalize_progress", side_effect=track_finalize), \
             patch.object(core.scan, "_mb_classify_and_order",
                          side_effect=_classify_passthrough):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
        self.assertEqual(call_order, ["finalize"])

    def test_classifier_always_runs_for_due_batch(self):
        """The MB pre-pass runs unconditionally -- no config flag selects it."""
        artists = [artist_payload("a1", "A")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]), \
             patch.object(core.scan, "_mb_classify_and_order",
                          side_effect=_classify_passthrough) as mock_classify:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)
        mock_classify.assert_called_once()


if __name__ == "__main__":
    unittest.main()

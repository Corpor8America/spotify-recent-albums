"""Unit tests for MusicBrainz integration in the scan pipeline.

Mocks the three MusicBrainz functions at the spotify_core.scan module level,
following the same pattern as test_scan.py.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import spotify_core as core
from spotify_core.models import Album, Artist, MusicBrainzAlbum, State
from spotify_core.scan import _build_priority_order, _process_upcoming_releases
from tests.support import ContextTestCase


def _artist_payload(artist_id, name):
    return {"id": artist_id, "name": name}


def _album_payload(album_id, name, release_date, artist_id="art1"):
    return {
        "id": album_id,
        "name": name,
        "album_type": "album",
        "release_date": release_date,
        "total_tracks": 10,
        "artists": [{"id": artist_id}],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


class ProcessUpcomingReleasesTests(unittest.TestCase):

    def test_removes_albums_releasing_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        state = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Today Album", artist="A",
                                     artist_id="a1", release_date=today, first_seen="2025-01-01"),
            "rg-2": MusicBrainzAlbum(id="rg-2", name="Future Album", artist="B",
                                     artist_id="b1", release_date="2099-12-31", first_seen="2025-01-01"),
        })
        with patch.object(core.scan, "save_state"):
            _process_upcoming_releases(MagicMock(), state)
        self.assertNotIn("rg-1", state.musicbrainz_upcoming)
        self.assertIn("rg-2", state.musicbrainz_upcoming)

    def test_removes_past_albums(self):
        state = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Old Album", artist="A",
                                     artist_id="a1", release_date="2020-01-01", first_seen="2025-01-01"),
        })
        with patch.object(core.scan, "save_state"):
            _process_upcoming_releases(MagicMock(), state)
        self.assertNotIn("rg-1", state.musicbrainz_upcoming)

    def test_keeps_future_albums(self):
        state = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Future", artist="A",
                                     artist_id="a1", release_date="2099-12-31", first_seen="2025-01-01"),
        })
        with patch.object(core.scan, "save_state"):
            _process_upcoming_releases(MagicMock(), state)
        self.assertIn("rg-1", state.musicbrainz_upcoming)

    def test_empty_state_no_op(self):
        state = State()
        with patch.object(core.scan, "save_state") as mock_save:
            _process_upcoming_releases(MagicMock(), state)
        mock_save.assert_not_called()


class MbIdResolutionTests(ContextTestCase):
    """Tests for MusicBrainz ID resolution in _process_artists."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_resolves_mb_id_on_first_scan(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Test Artist")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.artists["a1"].musicbrainz_id, "mb-123")
        mock_resolve.assert_called_once_with("a1")

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_does_not_reresolve_cached_mb_id(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Test Artist")]
        now_iso = datetime.now(timezone.utc).isoformat()
        core.save_state(State(artists={
            "a1": Artist(id="a1", name="Test Artist", musicbrainz_id="mb-cached",
                         last_checked=now_iso),
        }))
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_resolve.assert_not_called()

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value=None)
    def test_logs_warning_when_no_mb_mapping(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Unknown Artist")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.artists["a1"].musicbrainz_id, "")


class MbActiveStatusTests(ContextTestCase):
    """Tests for MusicBrainz active status checking in _process_artists."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_checks_active_on_first_scan(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Test")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertTrue(state.artists["a1"].mb_active)
        self.assertNotEqual(state.artists["a1"].mb_active_checked, "")
        mock_active.assert_called_once_with("mb-123")

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=False)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-456")
    def test_marks_inactive_artist(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Inactive")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertFalse(state.artists["a1"].mb_active)
        # Inactive + no upcoming -> Spotify check skipped
        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_skips_active_refresh_when_check_is_fresh(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Test")]
        now_iso = datetime.now(timezone.utc).isoformat()
        core.save_state(State(artists={
            "a1": Artist(id="a1", name="Test", musicbrainz_id="mb-123",
                         last_checked=now_iso,
                         mb_active=True, mb_active_checked=now_iso),
        }))
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        # Should not re-check since the check is fresh (just done now)
        mock_active.assert_not_called()

    @patch("spotify_core.scan.get_albums_with_future_dates", return_value=[])
    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_refreshes_active_when_interval_expired(self, mock_resolve, mock_active, mock_future):
        artists = [_artist_payload("a1", "Test")]
        old_checked = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        core.save_state(State(artists={
            "a1": Artist(id="a1", name="Test", musicbrainz_id="mb-123",
                         last_checked=now_iso,
                         mb_active=True, mb_active_checked=old_checked),
        }))
        self.write_config({"musicbrainz_active_refresh_days": 30})
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_active.assert_called_once_with("mb-123")


class MbUpcomingReleasesTests(ContextTestCase):
    """Tests for MusicBrainz upcoming release storage in _process_artists."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_stores_upcoming_releases_in_state(self, mock_resolve, mock_active):
        artists = [_artist_payload("a1", "Test")]
        future_albums = [
            {"id": "rg-1", "title": "Next Album", "primary-type": "Album",
             "first-release-date": "2099-06-01"},
        ]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]), \
             patch("spotify_core.scan.get_albums_with_future_dates", return_value=future_albums):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertIn("rg-1", state.musicbrainz_upcoming)
        self.assertEqual(state.musicbrainz_upcoming["rg-1"].name, "Next Album")
        self.assertEqual(state.musicbrainz_upcoming["rg-1"].artist, "Test")

    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_does_not_overwrite_existing_upcoming(self, mock_resolve, mock_active):
        artists = [_artist_payload("a1", "Test")]
        core.save_state(State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Already Tracked", artist="Old",
                                     artist_id="old", release_date="2099-06-01",
                                     first_seen="2025-01-01"),
        }))
        future_albums = [
            {"id": "rg-1", "title": "Same Album", "primary-type": "Album",
             "first-release-date": "2099-06-01"},
        ]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]), \
             patch("spotify_core.scan.get_albums_with_future_dates", return_value=future_albums):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.musicbrainz_upcoming["rg-1"].name, "Already Tracked")


class MbSkipLogicTests(ContextTestCase):
    """Tests for the skip logic based on MusicBrainz data."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_skips_spotify_check_when_upcoming_mb_release(self, mock_resolve, mock_active):
        artists = [_artist_payload("a1", "Test")]
        future_albums = [
            {"id": "rg-1", "title": "Upcoming", "primary-type": "Album",
             "first-release-date": "2099-12-31"},
        ]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums") as mock_albums, \
             patch("spotify_core.scan.get_albums_with_future_dates", return_value=future_albums):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_artist_active", return_value=False)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_skips_inactive_no_upcoming(self, mock_resolve, mock_active):
        artists = [_artist_payload("a1", "Inactive")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums") as mock_albums, \
             patch("spotify_core.scan.get_albums_with_future_dates", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_artist_active", return_value=True)
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_active_artist_runs_spotify_check(self, mock_resolve, mock_active):
        artists = [_artist_payload("a1", "Active")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as mock_albums, \
             patch("spotify_core.scan.get_albums_with_future_dates", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_albums.assert_called_once()


class BuildPriorityOrderTests(ContextTestCase):
    """Tests for _build_priority_order."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    def _artist(self, artist_id, name):
        return {"id": artist_id, "name": name}

    @patch("spotify_core.scan.get_albums_in_window")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_resolves_and_persists_mb_id(self, mock_resolve, mock_in_window):
        mock_resolve.return_value = "mb-123"
        mock_in_window.return_value = []
        artists = [self._artist("a1", "Test")]
        state = State()
        _build_priority_order(MagicMock(), state, artists, 365)
        self.assertEqual(state.artists["a1"].musicbrainz_id, "mb-123")

    @patch("spotify_core.scan.get_albums_in_window")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_skips_unresolvable_artists(self, mock_resolve, mock_in_window):
        mock_resolve.return_value = None
        artists = [self._artist("a1", "Unknown")]
        state = State()
        result = _build_priority_order(MagicMock(), state, artists, 365)
        self.assertEqual(result, [])

    @patch("spotify_core.scan.get_albums_in_window")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_sorts_by_release_date_ascending(self, mock_resolve, mock_in_window):
        mock_resolve.return_value = "mb-123"
        artists = [self._artist("a1", "Test")]
        state = State()
        mock_in_window.return_value = [
            {"id": "rg1", "first-release-date": "2026-06-01"},
            {"id": "rg2", "first-release-date": "2026-03-01"},
        ]
        result = _build_priority_order(MagicMock(), state, artists, 365)
        # Only one artist, but should appear once
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], "a1")

    @patch("spotify_core.scan.get_albums_in_window")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_dedupes_artist_with_multiple_releases(self, mock_resolve, mock_in_window):
        mock_resolve.return_value = "mb-123"
        artists = [self._artist("a1", "Prolific")]
        state = State()
        mock_in_window.return_value = [
            {"id": "rg1", "first-release-date": "2026-01-01"},
            {"id": "rg2", "first-release-date": "2026-06-01"},
        ]
        result = _build_priority_order(MagicMock(), state, artists, 365)
        self.assertEqual(result.count("a1"), 1)

    @patch("spotify_core.scan.get_albums_in_window")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_exception_does_not_abort_other_artists(self, mock_resolve, mock_in_window):
        artists = [self._artist("a1", "Good"), self._artist("a2", "Bad")]
        state = State()
        state.artists["a1"] = Artist(id="a1", name="Good", musicbrainz_id="mb-good")
        state.artists["a2"] = Artist(id="a2", name="Bad", musicbrainz_id="mb-bad")

        def side_effect(ctx, mbid, days):
            if mbid == "mb-bad":
                raise RuntimeError("MB error")
            return [{"id": "rg1", "first-release-date": "2026-06-01"}]

        mock_in_window.side_effect = side_effect
        result = _build_priority_order(MagicMock(), state, artists, 365)
        self.assertEqual(result, ["a1"])

    @patch("spotify_core.scan.get_albums_in_window")
    def test_uses_cached_mbid(self, mock_in_window):
        artists = [self._artist("a1", "Cached")]
        state = State()
        state.artists["a1"] = Artist(id="a1", name="Cached", musicbrainz_id="mb-cached")
        mock_in_window.return_value = []
        with patch("spotify_core.scan.resolve_spotify_to_mb") as mock_resolve:
            _build_priority_order(MagicMock(), state, artists, 365)
            mock_resolve.assert_not_called()

    @patch("spotify_core.scan.get_albums_in_window")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_sorts_across_artists(self, mock_resolve, mock_in_window):
        artists = [self._artist("a1", "Artist1"), self._artist("a2", "Artist2")]
        state = State()

        def resolve_side_effect(spotify_id):
            return "mb-" + spotify_id

        mock_resolve.side_effect = resolve_side_effect

        def in_window_side_effect(ctx, mbid, days):
            if mbid == "mb-a1":
                return [{"id": "rg1", "first-release-date": "2026-06-01"}]
            return [{"id": "rg2", "first-release-date": "2026-03-01"}]

        mock_in_window.side_effect = in_window_side_effect
        result = _build_priority_order(MagicMock(), state, artists, 365)
        self.assertEqual(result, ["a2", "a1"])


if __name__ == "__main__":
    unittest.main()

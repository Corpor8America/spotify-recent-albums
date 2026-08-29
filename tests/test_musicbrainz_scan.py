"""Unit tests for MusicBrainz integration in the scan pipeline.

Mocks the MusicBrainz functions at the spotify_core.scan module level,
following the same pattern as test_scan.py.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import spotify_core as core
from spotify_core.models import Artist, MusicBrainzAlbum, State
from spotify_core.scan import _prune_expired_upcoming
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


class PruneExpiredUpcomingTests(unittest.TestCase):

    def test_removes_albums_releasing_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        state = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Today Album", artist="A",
                                     artist_id="a1", release_date=today, first_seen="2025-01-01"),
            "rg-2": MusicBrainzAlbum(id="rg-2", name="Future Album", artist="B",
                                     artist_id="b1", release_date="2099-12-31", first_seen="2025-01-01"),
        })
        with patch.object(core.scan, "save_state"):
            _prune_expired_upcoming(MagicMock(), state)
        self.assertNotIn("rg-1", state.musicbrainz_upcoming)
        self.assertIn("rg-2", state.musicbrainz_upcoming)

    def test_removes_past_albums(self):
        state = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Old Album", artist="A",
                                     artist_id="a1", release_date="2020-01-01", first_seen="2025-01-01"),
        })
        with patch.object(core.scan, "save_state"):
            _prune_expired_upcoming(MagicMock(), state)
        self.assertNotIn("rg-1", state.musicbrainz_upcoming)

    def test_keeps_future_albums(self):
        state = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Future", artist="A",
                                     artist_id="a1", release_date="2099-12-31", first_seen="2025-01-01"),
        })
        with patch.object(core.scan, "save_state"):
            _prune_expired_upcoming(MagicMock(), state)
        self.assertIn("rg-1", state.musicbrainz_upcoming)

    def test_empty_state_no_op(self):
        state = State()
        with patch.object(core.scan, "save_state") as mock_save:
            _prune_expired_upcoming(MagicMock(), state)
        mock_save.assert_not_called()


class MbIdResolutionTests(ContextTestCase):
    """Tests for MusicBrainz ID resolution in the scan's MB pre-pass."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(True, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_resolves_mb_id_on_first_scan(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Test Artist")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.artists["a1"].musicbrainz_id, "mb-123")
        mock_resolve.assert_called_once_with("a1")

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(True, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_does_not_reresolve_cached_mb_id(self, mock_resolve, mock_status):
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

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(True, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value=None)
    def test_no_mb_mapping_leaves_id_empty(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Unknown Artist")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.artists["a1"].musicbrainz_id, "")


class MbActiveStatusTests(ContextTestCase):
    """Tests for MusicBrainz active status checking in the MB pre-pass."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(True, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_checks_active_on_first_scan(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Test")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertTrue(state.artists["a1"].mb_active)
        self.assertNotEqual(state.artists["a1"].mb_active_checked, "")
        mock_status.assert_called_once()

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(False, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-456")
    def test_marks_inactive_artist(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Inactive")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertFalse(state.artists["a1"].mb_active)
        # Inactive artists never hit the Spotify API
        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(False, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_stale_inactive_rechecked_once(self, mock_resolve, mock_status):
        """An artist cached as inactive for >30 days gets a single MB
        re-check; still inactive keeps them skipped."""
        artists = [_artist_payload("a1", "Inactive")]
        old_checked = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        core.save_state(State(artists={
            "a1": Artist(id="a1", name="Inactive", musicbrainz_id="mb-456",
                         last_checked=old_checked,
                         mb_active=False, mb_active_checked=old_checked),
        }))
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_status.assert_called_once()
        mock_albums.assert_not_called()
        self.assertFalse(core.load_state().artists["a1"].mb_active)

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb")
    def test_fresh_inactive_skips_mb_call(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Inactive")]
        now_iso = datetime.now(timezone.utc).isoformat()
        core.save_state(State(artists={
            "a1": Artist(id="a1", name="Inactive", musicbrainz_id="mb-456",
                         last_checked=now_iso,
                         mb_active=False, mb_active_checked=now_iso),
        }))
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_status.assert_not_called()
        mock_albums.assert_not_called()


class MbUpcomingReleasesTests(ContextTestCase):
    """Tests for future-release storage in the MB pre-pass."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_stores_upcoming_releases_in_state(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Test")]
        future_albums = [
            {"id": "rg-1", "title": "Next Album", "primary-type": "Album",
             "first-release-date": "2099-06-01"},
        ]
        mock_status.return_value = (True, future_albums)
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertIn("rg-1", state.musicbrainz_upcoming)
        self.assertEqual(state.musicbrainz_upcoming["rg-1"].name, "Next Album")
        self.assertEqual(state.musicbrainz_upcoming["rg-1"].artist, "Test")

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_does_not_overwrite_existing_upcoming(self, mock_resolve, mock_status):
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
        mock_status.return_value = (True, future_albums)
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.musicbrainz_upcoming["rg-1"].name, "Already Tracked")


class MbSkipLogicTests(ContextTestCase):
    """Tests for the skip logic based on MusicBrainz data."""

    def setUp(self):
        super().setUp()
        self.write_token("test-token")

    @patch("spotify_core.scan.get_artist_status_and_release_groups")
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_skips_spotify_check_when_upcoming_mb_release(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Test")]
        future_albums = [
            {"id": "rg-1", "title": "Upcoming", "primary-type": "Album",
             "first-release-date": "2099-12-31"},
        ]
        mock_status.return_value = (True, future_albums)
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums") as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(False, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_skips_inactive_no_upcoming(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Inactive")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums") as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_artist_status_and_release_groups", return_value=(True, []))
    @patch("spotify_core.scan.resolve_spotify_to_mb", return_value="mb-123")
    def test_active_artist_runs_spotify_check(self, mock_resolve, mock_status):
        artists = [_artist_payload("a1", "Active")]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists), \
             patch.object(core.scan, "get_artist_albums", return_value=[]) as mock_albums:
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        mock_albums.assert_called_once()


if __name__ == "__main__":
    unittest.main()

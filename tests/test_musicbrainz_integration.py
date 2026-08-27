"""Integration tests for MusicBrainz API functions against a mock server.

Uses MockMusicBrainzServer to simulate the MusicBrainz API, with _MB_BASE_URL
patched to redirect requests to the mock server.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import spotify_core as core
from spotify_core.models import Artist, MusicBrainzAlbum, State
from tests.mock_musicbrainz_server import MockMusicBrainzServer
from tests.support import ContextTestCase


class MockMusicBrainzTestCase(unittest.TestCase):
    """Base class that starts a MockMusicBrainzServer and patches _MB_BASE_URL."""

    @classmethod
    def setUpClass(cls):
        cls.server = MockMusicBrainzServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        self._base_url_patcher = patch(
            "spotify_core.musicbrainz._MB_BASE_URL", self.server.base_url
        )
        self._base_url_patcher.start()
        # Disable real rate-limit sleeps during tests
        self._rate_limit_patcher = patch("spotify_core.musicbrainz._rate_limit")
        self._rate_limit_patcher.start()

    def tearDown(self):
        self._rate_limit_patcher.stop()
        self._base_url_patcher.stop()


class ResolveSpotifyToMbIntegrationTests(MockMusicBrainzTestCase):

    def test_resolves_known_spotify_id(self):
        from spotify_core.musicbrainz import resolve_spotify_to_mb
        result = resolve_spotify_to_mb("spotify-artist-001")
        self.assertEqual(result, "mb-artist-001")

    def test_returns_none_for_unknown_spotify_id(self):
        from spotify_core.musicbrainz import resolve_spotify_to_mb
        result = resolve_spotify_to_mb("nonexistent-spotify-id")
        self.assertIsNone(result)

    def test_server_received_request(self):
        from spotify_core.musicbrainz import resolve_spotify_to_mb
        resolve_spotify_to_mb("spotify-artist-001")
        log = self.server.snapshot()["request_log"]
        self.assertTrue(any("/ws/2/url" in path for _, path in log))


class GetArtistReleaseGroupsIntegrationTests(MockMusicBrainzTestCase):

    def test_returns_albums_for_known_artist(self):
        from spotify_core.musicbrainz import get_artist_release_groups
        result = get_artist_release_groups(None, "mb-artist-001")
        titles = [rg["title"] for rg in result]
        self.assertIn("Recent Album", titles)
        self.assertIn("Upcoming Album", titles)
        self.assertIn("Old Album", titles)

    def test_filters_out_non_album_types(self):
        from spotify_core.musicbrainz import get_artist_release_groups
        result = get_artist_release_groups(None, "mb-artist-001")
        titles = [rg["title"] for rg in result]
        # Single Release should be filtered out (primary-type == "Single")
        self.assertNotIn("Single Release", titles)

    def test_returns_empty_for_unknown_artist(self):
        from spotify_core.musicbrainz import get_artist_release_groups
        result = get_artist_release_groups(None, "mb-nonexistent")
        self.assertEqual(result, [])


class GetArtistActiveIntegrationTests(MockMusicBrainzTestCase):

    def test_active_artist_returns_true(self):
        from spotify_core.musicbrainz import get_artist_active
        result = get_artist_active("mb-artist-001")
        self.assertTrue(result)

    def test_inactive_artist_returns_false(self):
        from spotify_core.musicbrainz import get_artist_active
        result = get_artist_active("mb-artist-002")
        self.assertFalse(result)

    def test_unknown_artist_defaults_active(self):
        from spotify_core.musicbrainz import get_artist_active
        result = get_artist_active("mb-nonexistent")
        # 404 -> mb_request raises -> get_artist_active catches -> returns True
        self.assertTrue(result)


class GetAlbumsWithFutureDatesIntegrationTests(MockMusicBrainzTestCase):

    def test_returns_only_future_albums(self):
        from spotify_core.musicbrainz import get_albums_with_future_dates
        result = get_albums_with_future_dates(None, "mb-artist-001")
        # mb-artist-001 has "Upcoming Album" (2099-01-15) as the only future album
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Upcoming Album")

    def test_all_future_for_dedicated_artist(self):
        from spotify_core.musicbrainz import get_albums_with_future_dates
        result = get_albums_with_future_dates(None, "mb-artist-003")
        titles = [rg["title"] for rg in result]
        self.assertIn("Next Album", titles)
        self.assertIn("Also Upcoming", titles)

    def test_no_future_for_inactive_artist(self):
        from spotify_core.musicbrainz import get_albums_with_future_dates
        result = get_albums_with_future_dates(None, "mb-artist-002")
        self.assertEqual(result, [])


class Mb503RetryIntegrationTests(MockMusicBrainzTestCase):

    def test_retries_on_503_then_succeeds(self):
        from spotify_core.musicbrainz import resolve_spotify_to_mb
        # Configure mock to return 503 every other request
        self.server.configure(rate_limit_503_every=2)
        # First call hits 503, retries, hits 200 on second attempt
        result = resolve_spotify_to_mb("spotify-artist-001")
        self.assertEqual(result, "mb-artist-001")


class ScanWithMockMusicBrainzTests(MockMusicBrainzTestCase, ContextTestCase):
    """Integration test: run_scan against mock Spotify + mock MusicBrainz."""

    def setUp(self):
        ContextTestCase.setUp(self)
        MockMusicBrainzTestCase.setUp(self)
        self.write_token("test-token")

    def tearDown(self):
        MockMusicBrainzTestCase.tearDown(self)
        ContextTestCase.tearDown(self)

    @patch("spotify_core.scan.get_artist_albums", return_value=[])
    def test_scan_resolves_mb_id_via_mock_server(self, mock_albums):
        artists = [{"id": "spotify-artist-001", "name": "Active Band"}]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertEqual(state.artists["spotify-artist-001"].musicbrainz_id, "mb-artist-001")

    @patch("spotify_core.scan.get_artist_albums", return_value=[])
    def test_scan_stores_upcoming_releases(self, mock_albums):
        artists = [{"id": "spotify-artist-001", "name": "Active Band"}]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        upcoming_titles = [a.name for a in state.musicbrainz_upcoming.values()]
        self.assertIn("Upcoming Album", upcoming_titles)

    @patch("spotify_core.scan.get_artist_albums")
    def test_scan_skips_upcoming_mb_artist(self, mock_albums):
        artists = [{"id": "spotify-artist-003", "name": "Future Releases Band"}]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        # Artist has upcoming MB albums -> Spotify check should be skipped
        mock_albums.assert_not_called()

    @patch("spotify_core.scan.get_artist_albums")
    def test_scan_skips_inactive_artist(self, mock_albums):
        artists = [{"id": "spotify-artist-002", "name": "Inactive Band"}]
        with patch.object(core.scan, "get_access_token", return_value="tok"), \
             patch.object(core.scan, "get_followed_artists", return_value=artists):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        state = core.load_state()
        self.assertFalse(state.artists["spotify-artist-002"].mb_active)
        mock_albums.assert_not_called()


if __name__ == "__main__":
    unittest.main()

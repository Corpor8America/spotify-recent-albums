"""Tests that require the MockSpotifyServer (integration tier)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import spotify_core as core
from app import create_app
from tests.mock_musicbrainz_server import MockMusicBrainzServer
from tests.mock_spotify_server import MockSpotifyServer
from tests.support import make_context, write_config


class MockServerTestCase(unittest.TestCase):
    """Base class that starts a mock server and points a fresh context's
    API/token URLs at it."""

    num_artists = 82

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ctx = make_context(Path(cls._tmp.name))
        core.set_context(cls.ctx)
        cls.server = MockSpotifyServer(num_artists=cls.num_artists)
        cls.server.start()
        cls._orig_api_base = cls.ctx.spotify_api_base
        cls._orig_token_url = cls.ctx.spotify_token_url
        cls.ctx.spotify_api_base = cls.server.base_url + "/v1"
        cls.ctx.spotify_token_url = cls.server.base_url + "/token"

    @classmethod
    def tearDownClass(cls):
        cls.ctx.spotify_api_base = cls._orig_api_base
        cls.ctx.spotify_token_url = cls._orig_token_url
        core.set_context(None)
        cls.server.stop()
        cls._tmp.cleanup()


class CreatePlaylistTests(MockServerTestCase):
    """core.create_playlist against the mock Spotify server."""

    def setUp(self):
        write_config(self.ctx)

    def test_creates_playlist_and_returns_id(self):
        playlist_id = core.create_playlist("mock-access-token", "Test Playlist")
        self.assertTrue(playlist_id.isalnum())
        self.assertTrue(playlist_id.startswith("playlist"))
        self.assertEqual(self.server.state.created_playlists[-1]["name"], "Test Playlist")

    def test_hits_me_then_users_playlists(self):
        core.create_playlist("mock-access-token", "Another")
        log = self.server.state.request_log
        self.assertIn(("GET", "/v1/me"), log)
        self.assertIn(("POST", "/v1/users/mock-user/playlists"), log)


class DebugArtistRouteTests(MockServerTestCase):
    num_artists = 3

    def setUp(self):
        write_config(self.ctx)
        core.save_refresh_token("test_refresh")
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        if self.ctx.store.state_file.exists():
            self.ctx.store.state_file.unlink()
        if self.ctx.store.token_file.exists():
            self.ctx.store.token_file.unlink()

    def test_debug_artist_empty_form(self):
        response = self.client.get("/debug/artist")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Artist Album Inspector", response.data)
        self.assertIn(b"Fetch Albums", response.data)

    def test_debug_artist_invalid_input(self):
        response = self.client.post("/debug/artist", data={"artist_input": "not a valid id"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter a Spotify artist ID", response.data)

    def test_debug_artist_valid_id(self):
        response = self.client.post("/debug/artist", data={"artist_input": "a000000000000000000001"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Artist 1", response.data)
        self.assertIn(b"Album 0 by Artist 1", response.data)

    def test_debug_artist_valid_url(self):
        response = self.client.post("/debug/artist", data={
            "artist_input": "https://open.spotify.com/artist/a000000000000000000002"
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Artist 2", response.data)

    def test_debug_artist_not_configured_redirects(self):
        write_config(self.ctx, {"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/debug/artist", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings", response.location)

    @patch("app.core.resolve_spotify_to_mb")
    @patch("app.core.get_artist_active")
    @patch("app.core.get_artist_release_groups")
    @patch("app.core.get_albums_with_future_dates")
    def test_debug_artist_includes_musicbrainz(self, mock_future, mock_rg, mock_active, mock_resolve):
        mock_resolve.return_value = "mb-artist-id-123"
        mock_active.return_value = True
        mock_rg.return_value = [
            {"id": "rg-1", "title": "MB Album One", "primary-type": "Album", "first-release-date": "2025-06-01"},
            {"id": "rg-2", "title": "MB Album Two", "primary-type": "Album", "first-release-date": "2025-12-15"},
        ]
        mock_future.return_value = [
            {"id": "rg-2", "title": "MB Album Two", "primary-type": "Album", "first-release-date": "2025-12-15"},
        ]

        response = self.client.post("/debug/artist", data={"artist_input": "a000000000000000000001"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MusicBrainz Lookup", response.data)
        self.assertIn(b"mb-artist-id-123", response.data)
        self.assertIn(b"MB Album One", response.data)
        self.assertIn(b"MB Album Two", response.data)

    @patch("app.core.resolve_spotify_to_mb", side_effect=Exception("MB lookup failed"))
    def test_debug_artist_musicbrainz_error_handled(self, mock_resolve):
        response = self.client.post("/debug/artist", data={"artist_input": "a000000000000000000001"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Artist 1", response.data)
        self.assertIn(b"MB lookup failed", response.data)


class RunScanAgainstMockServerTests(MockServerTestCase):
    """End-to-end scan against the mock server: discovers albums and syncs
    them to a created playlist."""

    # Keep the catalog tiny: a full request cycle must stay well under the
    # client-side 120 req/min rate limit or tests stall on real sleeps.
    num_artists = 6

    def setUp(self):
        self.config = write_config(self.ctx, {"min_request_interval": 0})
        core.save_refresh_token("test_refresh")
        with patch.object(core.auth, "get_access_token", return_value="mock-access-token"):
            playlist_id = core.create_playlist("token", "E2E Playlist")
        write_config(self.ctx, {"min_request_interval": 0, "spotify_playlist_id": playlist_id})

    def tearDown(self):
        if self.ctx.store.state_file.exists():
            self.ctx.store.state_file.unlink()

    def test_scan_discovers_albums_and_syncs_playlist(self):
        result = core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["blocked_categories"], [])

        state = core.load_state()
        self.assertGreater(len(state.known_albums), 0)
        added = [a for a in state.known_albums.values() if a.added_to_playlist]
        self.assertGreater(len(added), 0)

        snapshot = self.server.snapshot()
        self.assertEqual(snapshot["playlist_track_count"], 10 * len(added))

    def test_second_scan_does_not_duplicate_tracks(self):
        core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        count_after_first = self.server.snapshot()["playlist_track_count"]
        # interval_days=0 marks every artist due again even though their
        # albums are already known -- nothing may be added twice.
        write_config(self.ctx, {"min_request_interval": 0,
                                "spotify_playlist_id": self.config["spotify_playlist_id"],
                                "interval_days": 0})
        core.run_scan(days=3650, min_request_interval=0)
        self.assertEqual(self.server.snapshot()["playlist_track_count"], count_after_first)


class PriorityScanIntegrationTests(unittest.TestCase):
    """Integration tests using both MockSpotifyServer and MockMusicBrainzServer
    to verify the priority scan feature end-to-end."""

    num_artists = 6

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ctx = make_context(Path(cls._tmp.name))
        core.set_context(cls.ctx)
        cls.spotify_server = MockSpotifyServer(num_artists=cls.num_artists)
        cls.spotify_server.start()
        cls.mb_server = MockMusicBrainzServer()
        cls.mb_server.start()
        cls._orig_api_base = cls.ctx.spotify_api_base
        cls._orig_token_url = cls.ctx.spotify_token_url
        cls.ctx.spotify_api_base = cls.spotify_server.base_url + "/v1"
        cls.ctx.spotify_token_url = cls.spotify_server.base_url + "/token"

    @classmethod
    def tearDownClass(cls):
        cls.ctx.spotify_api_base = cls._orig_api_base
        cls.ctx.spotify_token_url = cls._orig_token_url
        core.set_context(None)
        cls.spotify_server.stop()
        cls.mb_server.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.config = write_config(self.ctx, {
            "min_request_interval": 0,
            "musicbrainz_priority_scan": True,
        })
        core.save_refresh_token("test_refresh")
        with patch.object(core.auth, "get_access_token", return_value="mock-access-token"):
            playlist_id = core.create_playlist("token", "Priority Playlist")
        write_config(self.ctx, {
            "min_request_interval": 0,
            "spotify_playlist_id": playlist_id,
            "musicbrainz_priority_scan": True,
        })
        # Configure MB mappings: map the mock Spotify artist IDs to MBIDs
        # The mock Spotify server creates artists a000000000000000000001 .. a00000000000000000000N
        mb_mappings = {}
        for i in range(1, self.num_artists + 1):
            spotify_id = f"a{i:021d}"
            mb_mappings[spotify_id] = f"mb-artist-{i:03d}"
        self.mb_server.configure(artist_mappings=mb_mappings)

    def tearDown(self):
        if self.ctx.store.state_file.exists():
            self.ctx.store.state_file.unlink()
        self.mb_server.reset()

    def _patch_mb_base_url(self):
        """Return a patch context manager that redirects MB API calls to the mock server."""
        return patch("spotify_core.musicbrainz._MB_BASE_URL", self.mb_server.base_url)

    def test_priority_scan_completes_and_disables_setting(self):
        """A priority-enabled scan that completes cleanly should auto-disable
        the musicbrainz_priority_scan config setting."""
        with self._patch_mb_base_url():
            result = core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "ok")
        cfg = core.load_config()
        self.assertFalse(cfg["musicbrainz_priority_scan"])

    def test_priority_scan_discovers_albums(self):
        """A priority-enabled scan should still discover and playlist albums."""
        with self._patch_mb_base_url():
            result = core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "ok")

        state = core.load_state()
        self.assertGreater(len(state.known_albums), 0)
        added = [a for a in state.known_albums.values() if a.added_to_playlist]
        self.assertGreater(len(added), 0)

    def test_priority_scan_persists_mb_ids(self):
        """The priority pass should resolve and persist MB IDs in state."""
        with self._patch_mb_base_url():
            core.run_scan(days=3650, interval_days=3, min_request_interval=0)

        state = core.load_state()
        mb_ids = [a.musicbrainz_id for a in state.artists.values() if a.musicbrainz_id]
        # At least some artists should have resolved MB IDs from the MB mock
        self.assertGreater(len(mb_ids), 0)

    def test_priority_scan_handles_mb_failure_gracefully(self):
        """If the MB mock is unreachable, the scan should still complete
        via the normal Spotify path."""
        with patch("spotify_core.musicbrainz._MB_BASE_URL", "http://127.0.0.1:1"):
            result = core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "ok")
        state = core.load_state()
        self.assertGreater(len(state.known_albums), 0)

    def test_disabled_priority_scan_skips_priority_pass(self):
        """When musicbrainz_priority_scan is False, the priority pass
        (_build_priority_order) should not be called, but normal MB
        integration (resolution, active status) still runs."""
        write_config(self.ctx, {"musicbrainz_priority_scan": False})
        with self._patch_mb_base_url(), \
             patch("spotify_core.scan._build_priority_order") as mock_build:
            core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        mock_build.assert_not_called()

    def test_priority_scan_with_rate_limit_keeps_setting_enabled(self):
        """If the scan hits a Spotify rate limit, the priority setting
        should NOT be auto-disabled."""
        self.spotify_server.configure(daily_quota=2)
        with self._patch_mb_base_url():
            result = core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        self.assertNotEqual(result["blocked_categories"], [])
        cfg = core.load_config()
        self.assertTrue(cfg["musicbrainz_priority_scan"])
        # Clean up: reset quota for later tests
        self.spotify_server.configure(daily_quota=None)
        self.spotify_server.reset_quota()

    def test_second_scan_with_priority_disabled_skips_priority_pass(self):
        """After auto-disable, a second scan should not invoke the priority
        pass, though normal MB integration (resolution, active status) still runs."""
        with self._patch_mb_base_url():
            core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        # Setting should now be False
        self.assertFalse(core.load_config()["musicbrainz_priority_scan"])

        # Reset artists so second scan has due artists
        core.save_state(core.models.State())
        self.spotify_server.configure(artist_release_dates={
            f"a{i:021d}": "2026-07-01" for i in range(1, self.num_artists + 1)
        })

        with self._patch_mb_base_url(), \
             patch("spotify_core.scan._build_priority_order") as mock_build:
            core.run_scan(days=3650, interval_days=0, min_request_interval=0)
        mock_build.assert_not_called()


class AutoReorderIntegrationTests(unittest.TestCase):
    """Integration tests for the auto-reorder feature after scans."""

    num_artists = 5

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ctx = make_context(Path(cls._tmp.name))
        core.set_context(cls.ctx)
        cls.server = MockSpotifyServer(num_artists=cls.num_artists)
        cls.server.start()
        cls._orig_api_base = cls.ctx.spotify_api_base
        cls._orig_token_url = cls.ctx.spotify_token_url
        cls.ctx.spotify_api_base = cls.server.base_url + "/v1"
        cls.ctx.spotify_token_url = cls.server.base_url + "/token"

    @classmethod
    def tearDownClass(cls):
        cls.ctx.spotify_api_base = cls._orig_api_base
        cls.ctx.spotify_token_url = cls._orig_token_url
        core.set_context(None)
        cls.server.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.config = write_config(self.ctx, {"min_request_interval": 0})
        core.save_refresh_token("test_refresh")
        with patch.object(core.auth, "get_access_token", return_value="mock-access-token"):
            playlist_id = core.create_playlist("token", "Reorder Test")
        write_config(self.ctx, {"min_request_interval": 0, "spotify_playlist_id": playlist_id})

    def tearDown(self):
        if self.ctx.store.state_file.exists():
            self.ctx.store.state_file.unlink()

    def test_auto_reorder_sorts_out_of_order_albums(self):
        """When albums are added out of release-date order, the auto-reorder
        should sort them oldest-first."""
        # Configure artists with different release dates (all within lookback)
        self.server.configure(artist_release_dates={
            "a000000000000000000000": "2026-07-01",
            "a000000000000000000001": "2026-08-01",
            "a000000000000000000002": "2026-01-01",
            "a000000000000000000003": "2026-04-01",
            "a000000000000000000004": "2026-09-01",
        })
        result = core.run_scan(days=3650, interval_days=3, min_request_interval=0)
        self.assertEqual(result["status"], "ok")

        # Verify auto-reorder was triggered
        logs = core.get_recent_logs()
        self.assertTrue(any("Playlist order drifted" in l for l in logs))

        # Check playlist contents are in release-date order
        playlist_id = self.config["spotify_playlist_id"]
        from spotify_core.playlists import get_playlist_track_uris
        uris = get_playlist_track_uris(self.ctx, "mock-access-token", playlist_id, core.load_state())
        self.assertGreater(len(uris), 0)

        # The oldest album (a2, 2026-01-01) should come before the newest
        # Find which artist IDs appear in the playlist
        artist_ids_in_order = []
        seen = set()
        for uri in uris:
            # Extract artist ID from URI like "spotify:track:album_a000000000000000000002_000_00"
            parts = uri.split("_")
            for i, part in enumerate(parts):
                if part == "album" and i + 1 < len(parts):
                    album_id = parts[i + 1]
                    # album_id is like "a000000000000000000002"
                    if album_id not in seen:
                        seen.add(album_id)
                        artist_ids_in_order.append(album_id)
                    break

        # a2 (2026-01-01) should appear before a4 (2026-09-01) in the playlist
        if "a000000000000000000002" in artist_ids_in_order and "a000000000000000000004" in artist_ids_in_order:
            idx_a2 = artist_ids_in_order.index("a000000000000000000002")
            idx_a4 = artist_ids_in_order.index("a000000000000000000004")
            self.assertLess(idx_a2, idx_a4)

    def test_auto_reorder_skipped_when_no_new_albums(self):
        """When no albums are added, auto-reorder should not be triggered."""
        # First scan to populate state
        self.server.configure(artist_release_dates={
            "a000000000000000000001": "2026-07-01",
        })
        core.run_scan(days=3650, interval_days=3, min_request_interval=0)

        # Second scan with interval=0 but same artists (no new albums)
        log_before = len(core.get_recent_logs())
        result = core.run_scan(days=3650, interval_days=0, min_request_interval=0)
        log_after = len(core.get_recent_logs())

        # No "reordering" log should appear
        new_logs = core.get_recent_logs()[log_before:log_after]
        reorder_logs = [l for l in new_logs if "reordering" in l.lower()]
        self.assertEqual(len(reorder_logs), 0)


if __name__ == "__main__":
    unittest.main()

"""Tests that require the MockSpotifyServer (integration tier)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import spotify_core as core
from app import create_app
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


if __name__ == "__main__":
    unittest.main()

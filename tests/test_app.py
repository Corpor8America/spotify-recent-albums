import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    import flask
except ImportError:
    flask = None

TEST_DIR = Path(tempfile.mkdtemp())

os.environ["DATA_DIR"] = str(TEST_DIR)
os.environ["RUN_SCHEDULER"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spotify_core as core

core.DATA_DIR = TEST_DIR
core.CONFIG_FILE = TEST_DIR / "app-config.json"
core.STATE_FILE = TEST_DIR / "spotify-state.json"
core.TOKEN_FILE = TEST_DIR / "spotify-token.json"
core.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

_config = {
    "spotify_client_id": "test_client_id",
    "spotify_client_secret": "test_client_secret",
    "spotify_playlist_id": "",
    "interval_days": 7,
    "min_request_interval": 0,
    "days_lookback": 365,
    "cron_schedule": "0 6 * * *",
    "public_base_url": "http://localhost:8080",
    "flask_secret_key": "test-secret-key-for-testing",
}
with open(core.CONFIG_FILE, "w") as f:
    json.dump(_config, f)

if flask is not None:
    from app import app


@unittest.skipIf(flask is None, "flask not installed")
class AppRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True
        self._clean_data_files()
        self._reset_config()

    def tearDown(self):
        self._clean_data_files()
        self._reset_config()

    def _clean_data_files(self):
        for p in [core.STATE_FILE, core.TOKEN_FILE]:
            if p.exists():
                p.unlink()

    def _reset_config(self):
        with open(core.CONFIG_FILE, "w") as f:
            json.dump(_config, f)

    def _write_config(self, overrides):
        cfg = dict(_config)
        cfg.update(overrides)
        with open(core.CONFIG_FILE, "w") as f:
            json.dump(cfg, f)

    def _write_token(self, token="test_refresh"):
        with open(core.TOKEN_FILE, "w") as f:
            json.dump({"refresh_token": token}, f)

    # --- dashboard -----------------------------------------------------------

    def test_dashboard_redirects_when_not_configured(self):
        self._write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings", response.location)

    def test_dashboard_renders_when_configured(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recent Albums", response.data)

    def test_dashboard_shows_not_connected_when_no_token(self):
        response = self.client.get("/")
        self.assertIn(b"Not connected", response.data)

    def test_dashboard_shows_connected_when_token_exists(self):
        self._write_token()
        response = self.client.get("/")
        self.assertIn(b"Connected", response.data)

    def test_dashboard_shows_upcoming_when_future_albums_exist(self):
        future_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        state = {
            "artists": {},
            "known_albums": {
                "u1": {"name": "Forthcoming", "artist": "Test Artist",
                       "artist_id": "art1", "type": "album",
                       "release_date": future_date,
                       "url": "https://open.spotify.com/album/u1",
                       "total_tracks": 10, "first_seen": "2026-08-01T00:00:00+00:00",
                       "auto_excluded": False, "manual_override": None,
                       "added_to_playlist": False, "track_uris": []},
            }
        }
        with open(core.STATE_FILE, "w") as f:
            json.dump(state, f)
        response = self.client.get("/")
        self.assertIn(b"Upcoming releases", response.data)
        self.assertIn(b"Forthcoming", response.data)

    def test_dashboard_hides_upcoming_when_no_future_albums(self):
        state = {
            "artists": {},
            "known_albums": {
                "a1": {"name": "Past Album", "artist": "Test Artist",
                       "artist_id": "art1", "type": "album",
                       "release_date": "2020-01-01",
                       "url": "https://open.spotify.com/album/a1",
                       "total_tracks": 10, "first_seen": "2026-08-01T00:00:00+00:00",
                       "auto_excluded": False, "manual_override": None,
                       "added_to_playlist": False, "track_uris": []},
            }
        }
        with open(core.STATE_FILE, "w") as f:
            json.dump(state, f)
        response = self.client.get("/")
        self.assertNotIn(b"Upcoming releases", response.data)

    # --- settings ------------------------------------------------------------

    def test_settings_page(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings", response.data)

    def test_settings_post_redirects(self):
        response = self.client.post("/settings", data={
            "spotify_client_id": "new_id",
            "spotify_client_secret": "new_secret",
            "spotify_playlist_id": "",
            "interval_days": "3",
            "min_request_interval": "10",
            "days_lookback": "365",
            "cron_schedule": "0 6 * * *",
    "public_base_url": "http://127.0.0.1:8081",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

    # --- create playlist ----------------------------------------------------

    def test_create_playlist_requires_connection(self):
        response = self.client.post("/create_playlist", data={"playlist_name": "X"})
        self.assertEqual(response.status_code, 400)

    @patch("app.core.get_access_token", return_value="mock-token")
    @patch("app.core.create_playlist", return_value="new_playlist_id")
    def test_create_playlist_saves_id_and_redirects(self, mock_create, mock_token):
        self._write_token()
        response = self.client.post("/create_playlist", data={"playlist_name": "My Picks"},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        mock_token.assert_called_once_with("test_client_id", "test_client_secret", "test_refresh")
        mock_create.assert_called_once_with("mock-token", "My Picks")
        self.assertEqual(core.load_config()["spotify_playlist_id"], "new_playlist_id")

    def test_settings_shows_create_button_when_connected(self):
        self._write_token()
        response = self.client.get("/settings")
        self.assertIn(b"Create playlist", response.data)

    def test_settings_hides_create_button_when_not_connected(self):
        response = self.client.get("/settings")
        self.assertNotIn(b"Create playlist", response.data)

    # --- login ---------------------------------------------------------------

    def test_login_redirects_to_spotify(self):
        response = self.client.get("/login", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.spotify.com", response.location)

    def test_login_returns_500_when_no_client_id(self):
        self._write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 500)

    # --- callback ------------------------------------------------------------

    def test_callback_missing_code(self):
        response = self.client.get("/callback")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Missing authorization code", response.data)

    def test_callback_with_error(self):
        response = self.client.get("/callback?error=access_denied")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"access_denied", response.data)

    # --- scan actions --------------------------------------------------------

    @patch("app.core.start_scan")
    def test_run_now_triggers_scan(self, mock_run):
        response = self.client.post("/run", follow_redirects=False)
        self.assertEqual(response.status_code, 302)

    @patch("app.core.cancel_scan")
    def test_cancel_calls_cancel(self, mock_cancel):
        response = self.client.post("/cancel", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        mock_cancel.assert_called_once()

    # --- album overrides -----------------------------------------------------

    def _init_state_with_album(self):
        state = {
            "artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {},
        }
        state["known_albums"]["alb1"] = {
            "name": "Test Album", "artist": "Test Artist",
            "release_date": "2026-07-01", "auto_excluded": False,
            "manual_override": None, "added_to_playlist": False,
        }
        core.save_state(state)

    def test_set_override_true(self):
        self._init_state_with_album()
        response = self.client.post("/albums/alb1/override", data={"value": "true"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        loaded = core.load_state()
        self.assertTrue(loaded["known_albums"]["alb1"]["manual_override"])

    def test_set_override_false(self):
        self._init_state_with_album()
        response = self.client.post("/albums/alb1/override", data={"value": "false"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        loaded = core.load_state()
        self.assertFalse(loaded["known_albums"]["alb1"]["manual_override"])

    def test_set_override_unknown_album(self):
        response = self.client.post("/albums/nonexistent/override", data={"value": "true"})
        self.assertEqual(response.status_code, 404)

    # --- status API ----------------------------------------------------------

    def test_status_returns_json(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("connected", data)
        self.assertIn("scan_running", data)
        self.assertIn("known_albums_count", data)

    def test_status_connected_false_when_no_token(self):
        response = self.client.get("/status")
        data = json.loads(response.data)
        self.assertFalse(data["connected"])

    def test_status_connected_true_with_token(self):
        self._write_token()
        response = self.client.get("/status")
        data = json.loads(response.data)
        self.assertTrue(data["connected"])

    # --- rate-limit banner ---------------------------------------------------

    def test_dashboard_shows_rate_limit_banner_from_persisted_state(self):
        import time as _time
        self._write_token()
        core.save_state({
            "artists": {},
            "known_albums": {},
            "in_progress": None,
            "rate_limits": {"GET /artists/{id}/albums": int(_time.time()) + 3600},
        })
        response = self.client.get("/")
        self.assertIn(b"Rate-limited", response.data)
        self.assertIn(b"GET /artists/{id}/albums", response.data)

    def test_dashboard_hides_rate_limit_banner_when_expired(self):
        import time as _time
        self._write_token()
        core.save_state({
            "artists": {},
            "known_albums": {},
            "in_progress": None,
            "rate_limits": {"GET /artists/{id}/albums": int(_time.time()) - 3600},
        })
        response = self.client.get("/")
        self.assertNotIn(b"Rate-limited", response.data)

    # --- artists page --------------------------------------------------------

    def test_artists_page_renders(self):
        self._write_token()
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Followed Artists", response.data)

    def test_artists_page_empty_state(self):
        self._write_token()
        core.save_state({
            "artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {},
        })
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No artists tracked yet", response.data)

    def test_artists_page_shows_artists(self):
        self._write_token()
        core.save_state({
            "artists": {
                "a1": {"name": "Radiohead", "last_checked": "2026-08-01T00:00:00+00:00", "scanned_with": "1.5.0"},
                "a2": {"name": "Bjork", "last_checked": "2026-07-15T00:00:00+00:00", "scanned_with": "1.5.0"},
            },
            "known_albums": {}, "in_progress": None, "rate_limits": {},
        })
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Radiohead", response.data)
        self.assertIn(b"Bjork", response.data)
        self.assertIn(b"Followed Artists (2)", response.data)

    def test_artists_page_scan_status(self):
        self._write_token()
        core.save_state({
            "artists": {
                "a1": {"name": "Radiohead", "last_checked": "2026-08-01T00:00:00+00:00", "scanned_with": "1.5.0"},
                "a2": {"name": "Bjork", "last_checked": "2026-07-15T00:00:00+00:00", "scanned_with": "1.5.0"},
            },
            "known_albums": {},
            "in_progress": {"due_ids": ["a2"], "processed_ids": ["a1"]},
            "rate_limits": {},
        })
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"pending", response.data)
        self.assertIn(b"done", response.data)

    def test_artists_page_redirects_when_not_configured(self):
        self._write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/artists", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings", response.location)

    # --- settings validation -------------------------------------------------

    def test_settings_rejects_bad_cron(self):
        response = self.client.post("/settings", data={
            "spotify_client_id": "id",
            "spotify_client_secret": "secret",
            "spotify_playlist_id": "",
            "interval_days": "3",
            "min_request_interval": "0",
            "days_lookback": "365",
            "cron_schedule": "invalid cron",
            "public_base_url": "http://localhost:8080",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid settings", response.data)

    def test_settings_rejects_bad_playlist_id(self):
        response = self.client.post("/settings", data={
            "spotify_client_id": "id",
            "spotify_client_secret": "secret",
            "spotify_playlist_id": "has spaces!@#",
            "interval_days": "3",
            "min_request_interval": "0",
            "days_lookback": "365",
            "cron_schedule": "0 6 * * *",
            "public_base_url": "http://localhost:8080",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid settings", response.data)

    def test_settings_rejects_negative_interval(self):
        response = self.client.post("/settings", data={
            "spotify_client_id": "id",
            "spotify_client_secret": "secret",
            "spotify_playlist_id": "",
            "interval_days": "0",
            "min_request_interval": "0",
            "days_lookback": "365",
            "cron_schedule": "0 6 * * *",
            "public_base_url": "http://localhost:8080",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid settings", response.data)

    def test_settings_rejects_too_few_cron_fields(self):
        response = self.client.post("/settings", data={
            "spotify_client_id": "id",
            "spotify_client_secret": "secret",
            "spotify_playlist_id": "",
            "interval_days": "3",
            "min_request_interval": "0",
            "days_lookback": "365",
            "cron_schedule": "0 6 * *",
            "public_base_url": "http://localhost:8080",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid settings", response.data)


# --- debug_artist route tests (require MockSpotifyServer) ---


try:
    from mock_spotify_server import MockSpotifyServer
    _has_mock = True
except ImportError:
    _has_mock = False


@unittest.skipIf(not _has_mock, "mock_spotify_server not importable")
class DebugArtistRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockSpotifyServer(num_artists=3)
        cls.server.start()
        cls._orig_api_base = core.SPOTIFY_API_BASE
        cls._orig_token_url = core.SPOTIFY_TOKEN_URL
        core.SPOTIFY_API_BASE = cls.server.base_url + "/v1"
        core.SPOTIFY_TOKEN_URL = cls.server.base_url + "/token"

    @classmethod
    def tearDownClass(cls):
        core.SPOTIFY_API_BASE = cls._orig_api_base
        core.SPOTIFY_TOKEN_URL = cls._orig_token_url
        cls.server.stop()

    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True
        self._clean_data_files()
        self._reset_config()
        self._write_token()

    def tearDown(self):
        self._clean_data_files()
        self._reset_config()

    def _clean_data_files(self):
        for p in [core.STATE_FILE, core.TOKEN_FILE]:
            if p.exists():
                p.unlink()

    def _reset_config(self):
        with open(core.CONFIG_FILE, "w") as f:
            json.dump(_config, f)

    def _write_token(self, token="test_refresh"):
        with open(core.TOKEN_FILE, "w") as f:
            json.dump({"refresh_token": token}, f)

    def _write_config(self, overrides):
        cfg = dict(_config)
        cfg.update(overrides)
        with open(core.CONFIG_FILE, "w") as f:
            json.dump(cfg, f)

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
        self._write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/debug/artist", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings", response.location)


def tearDownModule():
    shutil.rmtree(TEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

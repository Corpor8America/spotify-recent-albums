import json
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import spotify_core as core
from app import create_app
from spotify_core.models import Album, State
from tests.support import ContextTestCase


def full_album(album_id, name, release_date):
    return Album(
        id=album_id, name=name, artist="Test Artist", artist_id="art1", album_type="album",
        release_date=release_date, url=f"https://open.spotify.com/album/{album_id}",
        total_tracks=10, first_seen="2026-08-01T00:00:00+00:00",
    )


class AppRoutesTests(ContextTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    # --- dashboard -----------------------------------------------------------

    def test_dashboard_redirects_when_not_configured(self):
        self.write_config({"spotify_client_id": "", "spotify_client_secret": ""})
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
        self.write_token()
        response = self.client.get("/")
        self.assertIn(b"Connected", response.data)

    def test_dashboard_shows_upcoming_when_future_albums_exist(self):
        future_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        core.save_state(State(known_albums={
            "u1": full_album("u1", "Forthcoming", future_date),
        }))
        response = self.client.get("/")
        self.assertIn(b"Recent releases", response.data)
        self.assertIn(b"Forthcoming", response.data)

    def test_dashboard_hides_upcoming_when_no_future_albums(self):
        core.save_state(State(known_albums={
            "a1": full_album("a1", "Past Album", "2020-01-01"),
        }))
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

    def test_settings_roundtrips_musicbrainz_priority_scan(self):
        self.client.post("/settings", data={
            "spotify_client_id": "id",
            "spotify_client_secret": "secret",
            "spotify_playlist_id": "",
            "interval_days": "3",
            "min_request_interval": "10",
            "days_lookback": "365",
            "cron_schedule": "0 6 * * *",
            "public_base_url": "http://localhost:8080",
            "musicbrainz_priority_scan": "true",
        })
        config = core.load_config()
        self.assertTrue(config["musicbrainz_priority_scan"])

    def test_settings_unchecked_musicbrainz_priority_scan(self):
        self.write_config({"musicbrainz_priority_scan": True})
        self.client.post("/settings", data={
            "spotify_client_id": "id",
            "spotify_client_secret": "secret",
            "spotify_playlist_id": "",
            "interval_days": "3",
            "min_request_interval": "10",
            "days_lookback": "365",
            "cron_schedule": "0 6 * * *",
            "public_base_url": "http://localhost:8080",
        })
        config = core.load_config()
        self.assertFalse(config["musicbrainz_priority_scan"])

    def test_settings_page_shows_priority_scan_checkbox(self):
        response = self.client.get("/settings")
        self.assertIn(b"musicbrainz_priority_scan", response.data)
        self.assertIn(b"Prioritize artists", response.data)

    # --- create playlist -----------------------------------------------------

    def test_create_playlist_requires_connection(self):
        response = self.client.post("/create_playlist", data={"playlist_name": "X"})
        self.assertEqual(response.status_code, 400)

    @patch("app.core.get_access_token", return_value="mock-token")
    @patch("app.core.create_playlist", return_value="new_playlist_id")
    def test_create_playlist_saves_id_and_redirects(self, mock_create, mock_token):
        self.write_token()
        response = self.client.post("/create_playlist", data={"playlist_name": "My Picks"},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        mock_token.assert_called_once_with("test_client_id", "test_client_secret", "test_refresh")
        mock_create.assert_called_once_with("mock-token", "My Picks")
        self.assertEqual(core.load_config()["spotify_playlist_id"], "new_playlist_id")

    def test_settings_shows_create_button_when_connected(self):
        self.write_token()
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
        self.write_config({"spotify_client_id": "", "spotify_client_secret": ""})
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
        core.save_state(State(known_albums={
            "alb1": Album(id="alb1", name="Test Album", artist="Test Artist",
                          artist_id="art1", album_type="album", release_date="2026-07-01",
                          url="", total_tracks=10, first_seen=""),
        }))

    def test_set_override_true(self):
        self._init_state_with_album()
        response = self.client.post("/albums/alb1/override", data={"value": "true"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        loaded = core.load_state()
        self.assertTrue(loaded.known_albums["alb1"].manual_override)

    def test_set_override_false(self):
        self._init_state_with_album()
        response = self.client.post("/albums/alb1/override", data={"value": "false"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        loaded = core.load_state()
        self.assertFalse(loaded.known_albums["alb1"].manual_override)

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
        self.write_token()
        response = self.client.get("/status")
        data = json.loads(response.data)
        self.assertTrue(data["connected"])

    def test_status_in_progress_shape(self):
        from spotify_core.models import ScanProgress

        core.save_state(State(in_progress=ScanProgress(due_ids=["a1"], processed_ids=["a2"])))
        response = self.client.get("/status")
        data = json.loads(response.data)
        self.assertEqual(data["in_progress"], {"due_ids": ["a1"], "processed_ids": ["a2"]})

    # --- health endpoints ----------------------------------------------------

    def test_healthz_returns_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.data)["ok"])

    def test_readyz_ready_when_configured(self):
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.data)["ready"])

    def test_readyz_503_when_not_configured(self):
        self.write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.data)["ready"])

    # --- rate-limit banner ---------------------------------------------------

    def test_dashboard_shows_rate_limit_banner_from_persisted_state(self):
        self.write_token()
        core.save_state(State(
            rate_limits={"GET /artists/{id}/albums": int(time.time()) + 3600}))
        response = self.client.get("/")
        self.assertIn(b"Rate-limited", response.data)
        self.assertIn(b"GET /artists/{id}/albums", response.data)

    def test_dashboard_hides_rate_limit_banner_when_expired(self):
        self.write_token()
        core.save_state(State(
            rate_limits={"GET /artists/{id}/albums": int(time.time()) - 3600}))
        response = self.client.get("/")
        self.assertNotIn(b"Rate-limited", response.data)

    # --- artists page --------------------------------------------------------

    def test_artists_page_renders(self):
        self.write_token()
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Followed Artists", response.data)

    def test_artists_page_empty_state(self):
        self.write_token()
        core.save_state(State())
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No artists tracked yet", response.data)

    def test_artists_page_shows_artists(self):
        from spotify_core.models import Artist

        self.write_token()
        core.save_state(State(artists={
            "a1": Artist(id="a1", name="Radiohead", last_checked="2026-08-01T00:00:00+00:00", scanned_with="1.5.0"),
            "a2": Artist(id="a2", name="Bjork", last_checked="2026-07-15T00:00:00+00:00", scanned_with="1.5.0"),
        }))
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Radiohead", response.data)
        self.assertIn(b"Bjork", response.data)
        self.assertIn(b"Followed Artists (2)", response.data)

    def test_artists_page_scan_status(self):
        from spotify_core.models import Artist, ScanProgress

        self.write_token()
        core.save_state(State(
            artists={
                "a1": Artist(id="a1", name="Radiohead", last_checked="2026-08-01T00:00:00+00:00", scanned_with="1.5.0"),
                "a2": Artist(id="a2", name="Bjork", last_checked="2026-07-15T00:00:00+00:00", scanned_with="1.5.0"),
            },
            in_progress=ScanProgress(due_ids=["a2"], processed_ids=["a1"]),
        ))
        response = self.client.get("/artists")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"pending", response.data)
        self.assertIn(b"done", response.data)

    def test_artists_page_redirects_when_not_configured(self):
        self.write_config({"spotify_client_id": "", "spotify_client_secret": ""})
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


if __name__ == "__main__":
    unittest.main()

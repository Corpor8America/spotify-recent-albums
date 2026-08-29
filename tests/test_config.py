import json
import unittest
from pathlib import Path

import spotify_core as core
from tests.support import ContextTestCase, REPO_ROOT


class ConfigFileTests(ContextTestCase):
    def test_load_returns_defaults_when_missing(self):
        (self.tmp_path / "app-config.json").unlink()
        config = core.load_config()
        self.assertEqual(config["interval_days"], 3)
        self.assertEqual(config["days_lookback"], 365)
        self.assertIn("flask_secret_key", config)

    def test_save_and_load(self):
        config = {"spotify_client_id": "test", "spotify_client_secret": "secret",
                  "spotify_playlist_id": "", "interval_days": 14,
                  "min_request_interval": 10, "days_lookback": 180,
                  "cron_schedule": "0 6 * * *", "public_base_url": "http://test:8080",
                  "flask_secret_key": "key"}
        core.save_config(config)
        loaded = core.load_config()
        self.assertEqual(loaded["interval_days"], 14)

    def test_is_configured_false_when_missing(self):
        (self.tmp_path / "app-config.json").unlink()
        self.assertFalse(core.is_configured())

    def test_is_configured_true_when_set(self):
        self.assertTrue(core.is_configured())


class GetVersionTests(ContextTestCase):
    def test_returns_version_string(self):
        version = core.get_version()
        self.assertIsInstance(version, str)
        self.assertNotEqual(version, "")

    def test_returns_unknown_on_missing_file(self):
        self.ctx.version_file = Path("/nonexistent/VERSION")
        self.assertEqual(core.get_version(), "unknown")


if __name__ == "__main__":
    unittest.main()

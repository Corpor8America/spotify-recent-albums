"""Shared test support: context construction and config helpers.

Each test gets a fresh AppContext pointing at a temp directory, installed
as the default spotify_core context -- no monkey-patching of module
globals required.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("RUN_SCHEDULER", "0")

import spotify_core as core
from spotify_core import AppContext
from spotify_core.api import RateLimiter
from spotify_core.storage import JsonFileStore

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG = {
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


def make_context(root: Path) -> AppContext:
    return AppContext(
        data_dir=root,
        store=JsonFileStore(
            state_file=root / "spotify-state.json",
            token_file=root / "spotify-token.json",
            config_file=root / "app-config.json",
        ),
        version_file=REPO_ROOT / "VERSION",
        spotify_api_base="https://api.spotify.com/v1",
        spotify_auth_url="https://accounts.spotify.com/authorize",
        spotify_token_url="https://accounts.spotify.com/api/token",
        rate_limiter=RateLimiter(120, min_interval_seconds=0),
    )


def write_config(ctx, overrides=None):
    cfg = dict(DEFAULT_CONFIG)
    if overrides:
        cfg.update(overrides)
    ctx.store.save_config(cfg)
    return cfg


def write_token(ctx, token="test_refresh"):
    ctx.store.save_refresh_token(token)


class ContextTestCase(unittest.TestCase):
    """Base class giving every test a fresh temp-dir AppContext."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.ctx = make_context(self.tmp_path)
        core.set_context(self.ctx)
        self.config = write_config(self.ctx)

    def tearDown(self):
        core.set_context(None)
        self._tmp.cleanup()

    def write_config(self, overrides=None):
        self.config = write_config(self.ctx, overrides)

    def write_token(self, token="test_refresh"):
        write_token(self.ctx, token)

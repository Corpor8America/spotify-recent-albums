"""AppContext: all configurable/stateful dependencies in one object.

Functions across the package take an ``AppContext`` as their first
parameter instead of reading module globals, so tests (or a second
instance) can point the whole stack at a different data directory or API
base without monkey-patching.
"""

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .api import DEFAULT_MIN_REQUEST_INTERVAL_SECONDS, MAX_REQUESTS_PER_MINUTE, RateLimiter
from .storage import JsonFileStore


@dataclass
class AppContext:
    data_dir: Path
    store: JsonFileStore
    version_file: Path
    spotify_api_base: str
    spotify_auth_url: str
    spotify_token_url: str
    rate_limiter: RateLimiter

    @classmethod
    def from_env(cls) -> "AppContext":
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        repo_root = Path(__file__).resolve().parents[1]
        return cls(
            data_dir=data_dir,
            store=JsonFileStore(
                state_file=data_dir / "spotify-state.json",
                token_file=data_dir / "spotify-token.json",
                config_file=data_dir / "app-config.json",
            ),
            version_file=repo_root / "VERSION",
            spotify_api_base=os.environ.get("SPOTIFY_API_BASE_OVERRIDE", "https://api.spotify.com/v1"),
            spotify_auth_url=os.environ.get("SPOTIFY_AUTH_URL_OVERRIDE", "https://accounts.spotify.com/authorize"),
            spotify_token_url=os.environ.get("SPOTIFY_TOKEN_URL_OVERRIDE", "https://accounts.spotify.com/api/token"),
            rate_limiter=RateLimiter(MAX_REQUESTS_PER_MINUTE, DEFAULT_MIN_REQUEST_INTERVAL_SECONDS),
        )


_default_context = None
_context_lock = threading.Lock()


def get_context() -> AppContext:
    """Return the process-wide default context, creating it from the
    environment on first use (lazily -- importing spotify_core has no
    side effects)."""
    global _default_context
    if _default_context is None:
        with _context_lock:
            if _default_context is None:
                _default_context = AppContext.from_env()
    return _default_context


def set_context(ctx):
    """Replace the default context. Pass None to reset to lazy
    from-env creation. Intended for tests."""
    global _default_context
    with _context_lock:
        _default_context = ctx

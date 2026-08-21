"""spotify_core -- core Spotify logic for the containerized web app.

This package is an internal reorganization of the former single-module
``spotify_core.py``. ``import spotify_core as core`` keeps working: every
public function is re-exported below, bound to a lazily-created default
AppContext (built from the environment), so existing call sites and
patches like ``core.load_state()`` behave as before.

For tests or multi-instance embedding, build an AppContext explicitly
(see context.AppContext.from_env) and either pass it to the submodule
functions directly or install it with ``set_context``.
"""

import threading

from . import artists, auth, api, config, filters, playlists, reports, scan, state
from .api import (
    ALBUM_TRACKS_CATEGORY,
    ARTIST_ALBUMS_CATEGORY,
    DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    FOLLOWED_ARTISTS_CATEGORY,
    LONG_WAIT_THRESHOLD_SECONDS,
    MAX_REQUESTS_PER_MINUTE,
    PLAYLIST_ADD_CATEGORY,
    PLAYLIST_REMOVE_CATEGORY,
    RateLimiter,
    blocked_until,
    endpoint_category,
)
from .auth import (
    exchange_code_for_token,
    get_access_token,
    get_auth_url,
    is_connected,
    load_refresh_token,
    save_refresh_token,
)
from .config import (
    CHECK_INTERVAL_DAYS,
    DEFAULT_DAYS_LOOKBACK,
    get_version,
    is_configured,
    load_config,
    save_config,
)
from .context import AppContext, get_context, set_context
from .errors import (
    AuthError,
    ConfigError,
    LongRateLimitBlock,
    NotFoundError,
    RateLimitError,
    SpotifyAPIError,
    SpotifyCoreError,
)
from .filters import is_auto_excluded, is_effectively_excluded, parse_release_date
from .logging import clear_logs, configure_logging, get_recent_logs, log
from .models import Album, Artist, ScanProgress, State
from .playlists import (
    add_tracks_to_playlist,
    apply_album_override,
    create_playlist,
    get_album_track_uris,
    get_playlist_track_uris,
    prune_playlist,
    remove_tracks_from_playlist,
    reorder_playlist,
)
from .reports import get_excluded_albums, get_report_albums, get_upcoming_albums
from .scan import _cancel_event, cancel_scan, record_album, reorder_lock, run_lock, run_scan, start_scan
from .state import clear_expired_rate_limits, load_state, save_state, update_state

__all__ = [
    "Album", "Artist", "ScanProgress", "State", "AppContext",
    "SpotifyCoreError", "RateLimitError", "LongRateLimitBlock",
    "SpotifyAPIError", "AuthError", "ConfigError", "NotFoundError",
    "RateLimiter", "run_lock", "reorder_lock",
    "get_context", "set_context", "configure_logging",
]


def _ctx():
    return get_context()


# --- Public API bound to the default context ----------------------------------
#
# Each wrapper resolves the default context at call time (never at import
# time) so importing this package has no side effects and tests can swap
# the context via set_context().

def _bind(module, name):
    """Create a module-level wrapper for module.name that binds the
    default context as the first argument."""
    func = getattr(module, name)

    def wrapper(*args, **kwargs):
        return func(_ctx(), *args, **kwargs)

    wrapper.__name__ = name
    wrapper.__doc__ = func.__doc__
    return wrapper


load_config = _bind(config, "load_config")
save_config = _bind(config, "save_config")
is_configured = _bind(config, "is_configured")
get_version = _bind(config, "get_version")

load_state = _bind(state, "load_state")
save_state = _bind(state, "save_state")
update_state = _bind(state, "update_state")

get_auth_url = _bind(auth, "get_auth_url")
exchange_code_for_token = _bind(auth, "exchange_code_for_token")
get_access_token = _bind(auth, "get_access_token")
save_refresh_token = _bind(auth, "save_refresh_token")
load_refresh_token = _bind(auth, "load_refresh_token")
is_connected = _bind(auth, "is_connected")

spotify_request = _bind(api, "spotify_request")
spotify_get = _bind(api, "spotify_get")

get_followed_artists = _bind(artists, "get_followed_artists")
get_artist_albums = _bind(artists, "get_artist_albums")

get_album_track_uris = _bind(playlists, "get_album_track_uris")
get_playlist_track_uris = _bind(playlists, "get_playlist_track_uris")
add_tracks_to_playlist = _bind(playlists, "add_tracks_to_playlist")
remove_tracks_from_playlist = _bind(playlists, "remove_tracks_from_playlist")
prune_playlist = _bind(playlists, "prune_playlist")
reorder_playlist = _bind(playlists, "reorder_playlist")
create_playlist = _bind(playlists, "create_playlist")
apply_album_override = _bind(playlists, "apply_album_override")

start_scan = _bind(scan, "start_scan")
cancel_scan = _bind(scan, "cancel_scan")
run_scan = _bind(scan, "run_scan")


def endpoint_category(method, url):
    return api.endpoint_category(method, url, _ctx().spotify_api_base)


endpoint_category.__doc__ = api.endpoint_category.__doc__

"""Rate limiting and Spotify API request plumbing."""

import os
import re
import time
from datetime import datetime

import requests

from .errors import RateLimitError, SpotifyAPIError
from .logging import log

LONG_WAIT_THRESHOLD_SECONDS = 300
MAX_REQUESTS_PER_MINUTE = 120
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = float(os.environ.get("MIN_REQUEST_INTERVAL", "10"))

_ID_SEGMENT = re.compile(r"/[A-Za-z0-9]{15,}(?=/|$)")

# Rate-limit categories the scan needs, so it can skip a phase it already
# knows is blocked instead of starting it and stopping on the first request.
FOLLOWED_ARTISTS_CATEGORY = "GET /me/following"
ARTIST_ALBUMS_CATEGORY = "GET /artists/{id}/albums"
ALBUM_TRACKS_CATEGORY = "GET /albums/{id}/tracks"
PLAYLIST_ADD_CATEGORY = "POST /playlists/{id}/items"
PLAYLIST_REMOVE_CATEGORY = "DELETE /playlists/{id}/items"


def endpoint_category(method, url, api_base=""):
    """Normalize a request into its rate-limit category, e.g.
    ``GET /artists/{id}/albums``."""
    path = url.split("?", 1)[0]
    if api_base and path.startswith(api_base):
        path = path[len(api_base):]
    normalized = _ID_SEGMENT.sub("/{id}", path)
    return f"{method} {normalized}"


def blocked_until(state, category):
    """Return the retry-until timestamp for ``category`` if it is currently
    blocked (in the future), else None."""
    retry_until = state.rate_limits.get(category)
    if retry_until is None or int(retry_until) <= int(time.time()):
        return None
    return int(retry_until)


class RateLimiter:
    def __init__(self, max_requests, min_interval_seconds=0):
        self.max_requests = max_requests
        self.min_interval_seconds = min_interval_seconds
        self.timestamps = []
        self.last_request_time = None

    def wait_if_needed(self, label="request"):
        now = time.time()
        if self.min_interval_seconds and self.last_request_time is not None:
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval_seconds:
                sleep_time = self.min_interval_seconds - elapsed
                log(f"  Rate limiter: waiting {sleep_time:.1f}s before {label} (min {self.min_interval_seconds}s between requests)")
                time.sleep(sleep_time)
                now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.max_requests:
            sleep_time = 60 - (now - self.timestamps[0]) + 0.1
            if sleep_time > 0:
                log(f"  Rate limiter: waiting {sleep_time:.1f}s before {label} (hit {self.max_requests}/min limit)")
                time.sleep(sleep_time)
        self.last_request_time = time.time()
        self.timestamps.append(self.last_request_time)


def is_non_retryable_spotify_error(resp):
    if resp.status_code not in {400, 401, 403, 404, 422}:
        return False
    body = (resp.text or "").lower()
    return "invalid limit" in body or "invalid offset" in body or "invalid market" in body


def spotify_request(ctx, method, token, url, state, params=None, json_data=None, retries=5, backoff=1):
    category = endpoint_category(method, url, ctx.spotify_api_base)
    blocked_ts = state.rate_limits.get(category)
    if blocked_ts:
        if int(time.time()) < blocked_ts:
            raise RateLimitError(category, blocked_ts)
        del state.rate_limits[category]
        ctx.store.save_state(state)

    ctx.rate_limiter.wait_if_needed(f"{method} {url.split('?')[0]}")
    headers = {"Authorization": f"Bearer {token}"}
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, params=params, json=json_data,
                            timeout=(5, 30))

    if resp.status_code == 429:
        retry_after_raw = resp.headers.get("Retry-After", str(backoff))
        retry_after = int(retry_after_raw)
        if retry_after > LONG_WAIT_THRESHOLD_SECONDS:
            retry_until = int(time.time()) + retry_after
            state.rate_limits[category] = retry_until
            ctx.store.save_state(state)
            log(f"  Rate limited on {method} {url} (category: {category}); "
                f"Retry-After={retry_after_raw!r}; blocked for {retry_after}s "
                f"until {datetime.fromtimestamp(retry_until).astimezone().isoformat()}.")
            raise RateLimitError(category, retry_until)
        if retries <= 0:
            raise SpotifyAPIError(429, "Rate limited - max retries exceeded")
        wait = max(retry_after, backoff)
        time.sleep(wait)
        return spotify_request(ctx, method, token, url, state, params, json_data, retries - 1, wait * 2)

    if resp.status_code in (502, 503, 504):
        if retries <= 0:
            raise SpotifyAPIError(resp.status_code, f"Server error {resp.status_code} - max retries exceeded")
        time.sleep(backoff)
        return spotify_request(ctx, method, token, url, state, params, json_data, retries - 1, backoff * 2)

    if resp.status_code >= 400:
        if is_non_retryable_spotify_error(resp):
            raise SpotifyAPIError(resp.status_code, f"Spotify API request failed: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()

    if resp.status_code == 204:
        return {}
    return resp.json()


def spotify_get(ctx, token, url, state, params=None):
    return spotify_request(ctx, "GET", token, url, state, params=params)

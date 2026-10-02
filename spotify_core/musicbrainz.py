"""MusicBrainz API integration for upcoming album discovery and artist active status."""

import os
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .filters import parse_release_date
from .logging import log

# MusicBrainz's documented source-IP limit is 1 request/second *on average*
# (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting). The penalty is
# a cliff, not a token bucket: exceed the measured rate and MusicBrainz
# declines 100% of your requests until the rate decays back under -- so a
# client that keeps retrying keeps itself pinned. We stay a little above the
# ceiling (_MIN_INTERVAL), and _JITTER_SECONDS de-correlates from the boundary.
_MIN_INTERVAL = float(os.environ.get("MB_MIN_INTERVAL", "1.2"))
_JITTER_SECONDS = float(os.environ.get("MB_JITTER_SECONDS", "0.6"))
_MB_RETRIES = 3
# A server-supplied Retry-After is honoured, but capped so one long value
# can't park the scan thread for an unbounded stretch.
_MAX_RETRY_AFTER = float(os.environ.get("MB_MAX_RETRY_AFTER", "60"))

# MusicBrainz declines a request for three different reasons -- a throttle on
# our User-Agent, a throttle on our source IP, or the servers being globally
# overloaded -- and the 503 response does not say which. Retrying hard is only
# ever the right move for the IP case, so after _CIRCUIT_THRESHOLD consecutive
# 503s we stop asking entirely for _CIRCUIT_COOLDOWN seconds. That keeps a
# site-wide outage or a UA-level flag from turning into a retry storm.
_CIRCUIT_THRESHOLD = int(os.environ.get("MB_CIRCUIT_THRESHOLD", "3"))
_CIRCUIT_COOLDOWN = float(os.environ.get("MB_CIRCUIT_COOLDOWN", "300"))

# Serializes pacing and the shared ``_last_request_time`` so concurrent
# callers (the scan thread + a Flask request thread) can't both slip
# requests into the same second and draw a 503.
_rate_lock = threading.Lock()
_last_request_time = 0.0

# Guards the circuit-breaker counters below.
_circuit_lock = threading.Lock()
_consecutive_503 = 0
_circuit_open_until = 0.0

# How long a cached "inactive" verdict stays fresh before an artist's
# active status is re-checked with MusicBrainz.
MB_ACTIVE_REFRESH_DAYS = 30

_MB_BASE_URL = "https://musicbrainz.org"

# MusicBrainz's User-Agent rules: "Application name/<version> ( contact-url )",
# and there must be enough information in there for MetaBrainz to reach the
# maintainers if the app misbehaves. An uncontactable User-Agent is treated as
# anonymous and throttled far more aggressively, so the URL points at the real
# repository and the version tracks the VERSION file rather than drifting.
_PROJECT_URL = "https://github.com/Corpor8America/spotify-recent-albums"
_PROJECT_NAME = "SpotifyRecentlyReleasedAlbums"
_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def _read_version():
    try:
        return _VERSION_FILE.read_text().strip() or "0"
    except OSError:
        return "0"


_USER_AGENT = f"{_PROJECT_NAME}/{_read_version()} ({_PROJECT_URL})"


class MusicBrainzThrottled(Exception):
    """MusicBrainz is refusing our requests (503, or the circuit breaker is
    open). Distinct from a lookup that legitimately found nothing, so callers
    can avoid caching the failure as a real answer."""


def _rate_limit():
    """Sleep so MusicBrainz requests stay around 1/sec with jitter.

    Jitter keeps us off the exact 1/sec boundary. Thread-safe via
    ``_rate_lock``, so concurrent callers can't both slip requests into the
    same second and draw a 503.

    This must be called once per *request that reaches the wire*, retries
    included -- it is what advances the pacing clock, so skipping it on a
    retry lets the next request out before the floor has elapsed.
    """
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL + random.uniform(0, _JITTER_SECONDS) - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def _circuit_is_open():
    """True while the breaker is tripped and we should not be talking to
    MusicBrainz at all."""
    with _circuit_lock:
        return time.monotonic() < _circuit_open_until


def _record_503():
    """Count a 503 and trip the breaker once they pile up."""
    global _consecutive_503, _circuit_open_until
    with _circuit_lock:
        _consecutive_503 += 1
        if _consecutive_503 >= _CIRCUIT_THRESHOLD and _circuit_open_until <= time.monotonic():
            _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN
            log(f"MusicBrainz: {_consecutive_503} consecutive 503s -- "
                f"pausing requests for {_CIRCUIT_COOLDOWN:.0f}s")


def _record_success():
    """Any non-503 response means we are being served again."""
    global _consecutive_503, _circuit_open_until
    with _circuit_lock:
        _consecutive_503 = 0
        _circuit_open_until = 0.0


def reset_rate_limit_state():
    """Clear the pacing clock and the circuit breaker.

    Process-wide state normally only decays on its own; tests need to start
    from a known-served state so one test's simulated outage cannot fail every
    test that follows it.
    """
    global _last_request_time, _consecutive_503, _circuit_open_until
    with _rate_lock:
        _last_request_time = 0.0
    with _circuit_lock:
        _consecutive_503 = 0
        _circuit_open_until = 0.0


def _503_retry_wait(attempt, retry_after_raw=None):
    """How long to wait before retrying a 503: honours Retry-After (capped at
    _MAX_RETRY_AFTER so one long value can't stall the scan thread), but never
    retries instantly when it is absent or 0.

    This backoff alone does not guarantee the 1/sec floor -- attempt 0 can be as
    short as 1.0s. _rate_limit() is what actually enforces the floor, since it
    runs immediately before every attempt.
    """
    try:
        retry_after = int(retry_after_raw or 0)
    except (TypeError, ValueError):
        retry_after = 0
    retry_after = min(retry_after, _MAX_RETRY_AFTER)
    return max(retry_after, (2 ** attempt) + random.uniform(0, 1))


def mb_request(url, params=None):
    """Make a rate-limited GET request to MusicBrainz with JSON parsing and 503 retry.

    Raises MusicBrainzThrottled when MusicBrainz declines the request (503 after
    retries, or an already-open circuit). Callers must not treat that as a
    lookup that simply found nothing.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    for attempt in range(_MB_RETRIES):
        if _circuit_is_open():
            raise MusicBrainzThrottled("circuit breaker open after repeated 503s")
        # Paced per attempt, not per call: a retry is still a request that
        # counts against the 1/sec IP budget.
        _rate_limit()
        resp = requests.get(url, params=params, headers=headers, timeout=(5, 30))
        if resp.status_code == 503:
            _record_503()
            wait = _503_retry_wait(attempt, resp.headers.get("Retry-After"))
            log(f"MusicBrainz 503, retrying in {wait:.1f}s ({attempt + 1}/{_MB_RETRIES})...")
            time.sleep(wait)
            continue
        _record_success()
        # Only non-503 errors reach here; 503 is handled and retried above.
        resp.raise_for_status()
        return resp.json()
    raise MusicBrainzThrottled(f"MusicBrainz returned 503 on all {_MB_RETRIES} attempts")


def resolve_spotify_to_mb(spotify_artist_id):
    """Resolve a Spotify artist ID to a MusicBrainz artist MBID via URL lookup."""
    url = f"{_MB_BASE_URL}/ws/2/url"
    params = {
        "resource": f"https://open.spotify.com/artist/{spotify_artist_id}",
        "inc": "artist-rels",
        "fmt": "json",
    }
    try:
        data = mb_request(url, params)
    except MusicBrainzThrottled:
        # Let the caller stop the pass; a throttled lookup is not "no match".
        raise
    except Exception as e:
        log(f"MB: MusicBrainz lookup failed for {spotify_artist_id}: {e}")
        return None
    relations = data.get("relations", [])
    for rel in relations:
        if rel.get("target-type") == "artist" and "artist" in rel:
            mbid = rel["artist"].get("id")
            if mbid:
                return mbid
    return None


def get_artist_release_groups(ctx, mbid):
    """Get all release-groups of type 'album' for an artist, with pagination."""
    albums = []
    offset = 0
    limit = 100
    while True:
        url = f"{_MB_BASE_URL}/ws/2/artist/{mbid}"
        params = {
            "inc": "release-groups",
            "limit": limit,
            "offset": offset,
            "fmt": "json",
        }
        data = mb_request(url, params)
        release_groups = data.get("release-groups", [])
        for rg in release_groups:
            if rg.get("primary-type") == "Album":
                albums.append(rg)
        if len(release_groups) < limit:
            break
        offset += limit
    return albums


def get_artist_active(mbid):
    """Check if an artist is still active. Returns True if active (life_span.ended is False)."""
    url = f"{_MB_BASE_URL}/ws/2/artist/{mbid}"
    params = {"fmt": "json"}
    try:
        data = mb_request(url, params)
    except MusicBrainzThrottled:
        raise
    except Exception as e:
        log(f"MB: Failed to check active status for {mbid}: {e}")
        return True  # Assume active on error
    life_span = data.get("life_span", {})
    ended = life_span.get("ended", False)
    return not ended


def get_artist_status_and_release_groups(ctx, mbid):
    """Fetch an artist's active status and album release-groups in one call.

    Returns ``(active, release_groups)`` where ``active`` is True when the
    artist is not marked ended in MusicBrainz and ``release_groups`` is a list
    of release-groups of type 'album'. Assumes active / returns empty on error
    so a failure never blocks the scan -- but MusicBrainzThrottled propagates,
    because "MusicBrainz refused us" must not be cached as "artist is active".
    """
    active = True
    release_groups = []
    offset = 0
    limit = 100
    while True:
        url = f"{_MB_BASE_URL}/ws/2/artist/{mbid}"
        params = {
            "inc": "release-groups",
            "fmt": "json",
            "limit": limit,
            "offset": offset,
        }
        try:
            data = mb_request(url, params)
        except MusicBrainzThrottled:
            raise
        except Exception as e:
            log(f"MB: Failed to fetch status/release-groups for {mbid}: {e}")
            break
        if offset == 0:
            life_span = data.get("life_span", {})
            active = not life_span.get("ended", False)
        raw_groups = data.get("release-groups", [])
        groups = [rg for rg in raw_groups if rg.get("primary-type") == "Album"]
        release_groups.extend(groups)
        # Page on the *raw* count. Comparing the filtered Album count against
        # the page size ends pagination early for artists whose release-groups
        # include non-Album types (EPs, Singles), silently truncating the page.
        if len(raw_groups) < limit:
            break
        offset += limit
    return active, release_groups


def get_albums_with_future_dates(ctx, mbid):
    """Get release-groups with first-release-date > today."""
    today = datetime.now().strftime("%Y-%m-%d")
    release_groups = get_artist_release_groups(ctx, mbid)
    upcoming = []
    for rg in release_groups:
        release_date = rg.get("first-release-date", "")
        if release_date and release_date > today:
            upcoming.append(rg)
    return upcoming


def get_albums_in_window(ctx, mbid, days_lookback):
    """Get release-groups with first-release-date within
    [today - days_lookback, today], inclusive. Used to prioritize a large
    backlog scan; not a substitute for the Spotify-side date check."""
    cutoff = datetime.now() - timedelta(days=days_lookback)
    now = datetime.now()
    release_groups = get_artist_release_groups(ctx, mbid)
    in_window = []
    for rg in release_groups:
        parsed = parse_release_date(rg.get("first-release-date", ""))
        if parsed is not None and cutoff <= parsed <= now:
            in_window.append(rg)
    return in_window

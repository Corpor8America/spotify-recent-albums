"""MusicBrainz API integration for upcoming album discovery and artist active status."""

import random
import threading
import time
from datetime import datetime, timedelta

import requests

from .filters import parse_release_date
from .logging import log

# MusicBrainz requires 1 request/second minimum interval per source IP. We
# keep a little headroom above that hard ceiling -- it runs on a shared IP
# in Docker and its burst tolerance is only 1, so sitting right at 1.0s
# invites 503s. _JITTER_SECONDS also de-correlates from the exact boundary.
_MIN_INTERVAL = 1.2
_JITTER_SECONDS = 0.6
_MB_RETRIES = 3

# Serializes pacing and the shared ``_last_request_time`` so concurrent
# callers (the scan thread + a Flask request thread) can't both slip
# requests into the same second and draw a 503.
_rate_lock = threading.Lock()
_last_request_time = 0.0

# How long a cached "inactive" verdict stays fresh before an artist's
# active status is re-checked with MusicBrainz.
MB_ACTIVE_REFRESH_DAYS = 30

_MB_BASE_URL = "https://musicbrainz.org"

_USER_AGENT = "SpotifyRecentlyReleasedAlbums/1.0 (https://github.com/anomalyco/Spotify-Recently-Released-Albums)"


def _rate_limit():
    """Sleep so MusicBrainz requests stay around 1/sec with jitter.

    Jitter keeps us off the exact 1/sec boundary (MusicBrainz's burst
    tolerance is 1, so request-clock alignment alone can draw 503s).
    Thread-safe via ``_rate_lock``, so concurrent callers can't both
    slip through in the same instant.
    """
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL + random.uniform(0, _JITTER_SECONDS) - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def _503_retry_wait(attempt, retry_after_raw=None):
    """How long to wait before retrying a 503: honours Retry-After, but
    never retries instantly when it is absent or 0."""
    try:
        retry_after = int(retry_after_raw or 0)
    except (TypeError, ValueError):
        retry_after = 0
    return max(retry_after, (2 ** attempt) + random.uniform(0, 1))


def mb_request(url, params=None):
    """Make a rate-limited GET request to MusicBrainz with JSON parsing and 503 retry."""
    _rate_limit()
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    for attempt in range(_MB_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 503:
                wait = _503_retry_wait(attempt, resp.headers.get("Retry-After"))
                log(f"MusicBrainz 503, retrying in {wait:.1f}s ({attempt + 1}/{_MB_RETRIES})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 503 and attempt < _MB_RETRIES - 1:
                wait = _503_retry_wait(attempt)
                log(f"MusicBrainz 503, retrying in {wait:.1f}s ({attempt + 1}/{_MB_RETRIES})...")
                time.sleep(wait)
                continue
            raise
    return None


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
        if data is None:
            return None
        relations = data.get("relations", [])
        for rel in relations:
            if rel.get("target-type") == "artist" and "artist" in rel:
                mbid = rel["artist"].get("id")
                if mbid:
                    return mbid
    except Exception as e:
        log(f"MB: MusicBrainz lookup failed for {spotify_artist_id}: {e}")
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
        if data is None:
            break
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
        if data is None:
            return True  # Assume active if we can't determine
        life_span = data.get("life_span", {})
        ended = life_span.get("ended", False)
        return not ended
    except Exception as e:
        log(f"MB: Failed to check active status for {mbid}: {e}")
        return True  # Assume active on error


def get_artist_status_and_release_groups(ctx, mbid):
    """Fetch an artist's active status and album release-groups in one call.

    Returns ``(active, release_groups)`` where ``active`` is True when the
    artist is not marked ended in MusicBrainz and ``release_groups`` is a list
    of release-groups of type 'album'. Assumes active / returns empty on error
    so a failure never blocks the scan.
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
        except Exception as e:
            log(f"MB: Failed to fetch status/release-groups for {mbid}: {e}")
            break
        if data is None:
            break
        if offset == 0:
            life_span = data.get("life_span", {})
            active = not life_span.get("ended", False)
        groups = [
            rg for rg in data.get("release-groups", [])
            if rg.get("primary-type") == "Album"
        ]
        release_groups.extend(groups)
        if len(groups) < limit:
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

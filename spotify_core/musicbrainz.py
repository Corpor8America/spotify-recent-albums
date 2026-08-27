"""MusicBrainz API integration for upcoming album discovery and artist active status."""

import time
from datetime import datetime

import requests

from .logging import log

# MusicBrainz requires 1 request/second minimum interval
_last_request_time = 0.0
_MIN_INTERVAL = 1.0

_MB_BASE_URL = "https://musicbrainz.org"

_USER_AGENT = "SpotifyRecentlyReleasedAlbums/1.0 (https://github.com/anomalyco/Spotify-Recently-Released-Albums)"


def _rate_limit():
    global _last_request_time
    now = time.monotonic()
    wait = _MIN_INTERVAL - (now - _last_request_time)
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


def mb_request(url, params=None):
    """Make a rate-limited GET request to MusicBrainz with JSON parsing and 503 retry."""
    _rate_limit()
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 503:
                retry_after = int(resp.headers.get("Retry-After", 5))
                log(f"MusicBrainz 503, retrying in {retry_after}s...")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 503 and attempt < 2:
                time.sleep(5)
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

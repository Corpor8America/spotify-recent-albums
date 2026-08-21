"""Followed-artist fetching, due-artist selection, artist discographies."""

from datetime import datetime, timedelta, timezone

from .api import spotify_get


def get_followed_artists(ctx, token, state):
    artists = []
    url = f"{ctx.spotify_api_base}/me/following"
    params = {"type": "artist", "limit": 50}
    while True:
        data = spotify_get(ctx, token, url, state, params)
        items = data.get("artists", {}).get("items", [])
        if not items:
            break
        artists.extend(items)
        after = data.get("artists", {}).get("cursors", {}).get("after")
        if not after:
            break
        params["after"] = after
    return artists


def get_artist_albums(ctx, token, artist_id, state, market="US"):
    albums = []
    url = f"{ctx.spotify_api_base}/artists/{artist_id}/albums"
    limit, offset = 10, 0
    while offset < 1000:
        params = {"include_groups": "album", "limit": limit, "offset": offset, "market": market}
        data = spotify_get(ctx, token, url, state, params)
        items = data.get("items", [])
        if not items:
            break
        albums.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return albums


def get_due_artists(artists, state, interval_days):
    """Pick the artists due for a check. If none are overdue (small
    libraries), fall back to the oldest-checked batch so every artist is
    visited roughly once per interval."""
    now = datetime.now(timezone.utc)
    due = []
    for artist in artists:
        entry = state.artists.get(artist["id"])
        if entry is None:
            due.append(artist)
            continue
        last_checked = datetime.fromisoformat(entry.last_checked)
        if now - last_checked >= timedelta(days=interval_days):
            due.append(artist)
    if not due and artists:
        checked = []
        for artist in artists:
            entry = state.artists.get(artist["id"])
            if entry is not None:
                checked.append((artist, datetime.fromisoformat(entry.last_checked)))
        checked.sort(key=lambda x: x[1])
        batch_size = max(1, len(artists) // max(1, interval_days))
        due = [artist for artist, _ in checked[:min(len(checked), batch_size)]]
    return due

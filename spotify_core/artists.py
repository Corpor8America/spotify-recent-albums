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
    """Pick artists due for a Spotify check.

    Artists with a future MusicBrainz release are deliberately deferred
    until that release date is reached. Such artists may have an empty
    ``last_checked`` because they have never needed a Spotify scan.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    deferred_ids = {
        album.artist_id
        for album in state.musicbrainz_upcoming.values()
        if album.release_date > today
    }

    due = []
    checked = []
    for artist in artists:
        artist_id = artist["id"]
        if artist_id in deferred_ids:
            continue

        entry = state.artists.get(artist_id)
        if entry is None:
            due.append(artist)
            continue

        # An empty last_checked is valid for an artist discovered by
        # MusicBrainz but not yet checked by Spotify. If there is no
        # future release deferring it, the artist needs a first Spotify scan.
        if not entry.last_checked:
            due.append(artist)
            continue

        last_checked = datetime.fromisoformat(entry.last_checked)
        if now - last_checked >= timedelta(days=interval_days):
            due.append(artist)
        else:
            checked.append((artist, last_checked))

    if not due and checked:
        checked.sort(key=lambda x: x[1])
        batch_size = max(1, len(checked) // max(1, interval_days))
        due = [artist for artist, _ in checked[:min(len(checked), batch_size)]]

    return due

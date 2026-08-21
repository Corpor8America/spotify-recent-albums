"""Playlist operations: track sync, pruning, reordering, creation,
and the manual include/exclude override flow."""

from datetime import datetime, timedelta

from . import auth as auth_mod
from . import config as config_mod
from . import state as state_mod
from .api import spotify_get, spotify_request
from .filters import is_effectively_excluded, parse_release_date
from .logging import log
from .models import State


def get_album_track_uris(ctx, token, album_id, state):
    uris = []
    url = f"{ctx.spotify_api_base}/albums/{album_id}/tracks"
    limit, offset = 50, 0
    while True:
        data = spotify_get(ctx, token, url, state, {"limit": limit, "offset": offset})
        items = data.get("items", [])
        if not items:
            break
        uris.extend(item["uri"] for item in items)
        if len(items) < limit:
            break
        offset += limit
    return uris


def get_playlist_track_uris(ctx, token, playlist_id, state):
    """Return every track URI currently in a playlist, including duplicates."""
    uris = []
    url = f"{ctx.spotify_api_base}/playlists/{playlist_id}/items"
    limit, offset = 100, 0
    while True:
        data = spotify_get(ctx, token, url, state, {"limit": limit, "offset": offset})
        items = data.get("items", [])
        uris.extend(
            item["track"]["uri"] for item in items
            if item.get("track") and item["track"].get("uri")
        )
        total = data.get("total")
        if not items or len(items) < limit or (total is not None and len(uris) >= total):
            break
        offset += limit
    return uris


def add_tracks_to_playlist(ctx, token, playlist_id, track_uris, state):
    url = f"{ctx.spotify_api_base}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        spotify_request(ctx, "POST", token, url, state, json_data={"uris": track_uris[i:i + 100]})


def remove_tracks_from_playlist(ctx, token, playlist_id, track_uris, state):
    url = f"{ctx.spotify_api_base}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        items = [{"uri": u} for u in track_uris[i:i + 100]]
        spotify_request(ctx, "DELETE", token, url, state, json_data={"items": items})


def prune_playlist(ctx, token, state, days, playlist_id):
    """Remove tracks of aged-out or excluded albums from the playlist.
    Tracks shared with a kept album are never removed."""
    if not playlist_id:
        return
    cutoff = datetime.now() - timedelta(days=days)
    removal_ids, keep_uris = [], set()
    for album_id, album in state.known_albums.items():
        if not album.added_to_playlist:
            continue
        release_date = parse_release_date(album.release_date)
        aged_out = release_date is not None and release_date < cutoff
        excluded = is_effectively_excluded(album)
        if aged_out or excluded:
            removal_ids.append(album_id)
        else:
            keep_uris.update(album.track_uris or [])
    if not removal_ids:
        return
    log(f"Pruning {len(removal_ids)} album(s) from playlist (aged-out or excluded)...")
    for album_id in removal_ids:
        album = state.known_albums[album_id]
        track_uris = album.track_uris
        if not track_uris:
            try:
                track_uris = get_album_track_uris(ctx, token, album_id, state)
            except Exception as e:
                log(f"  ERROR fetching tracks for '{album.name}' during prune: {e}")
                continue
        to_remove = [u for u in track_uris if u not in keep_uris]
        if to_remove:
            try:
                remove_tracks_from_playlist(ctx, token, playlist_id, to_remove, state)
                log(f"  Removed {len(to_remove)} track(s) from '{album.name}'")
            except Exception as e:
                log(f"  ERROR removing '{album.name}' from playlist: {e}")
                continue
        album.added_to_playlist = False
        album.track_uris = []
        state_mod.save_state(ctx, state)


def reorder_playlist(ctx, token, state, playlist_id):
    """Reorders the playlist so tracks are sorted by album release date
    (oldest first). Deletes all current tracks and re-adds them in the
    desired order."""
    if not playlist_id:
        return

    albums = [
        a for a in state.known_albums.values()
        if a.added_to_playlist and not is_effectively_excluded(a)
    ]

    def sort_key(album):
        parsed = parse_release_date(album.release_date)
        return parsed if parsed is not None else datetime.min

    albums.sort(key=sort_key)

    ordered_uris = []
    for album in albums:
        ordered_uris.extend(album.track_uris or [])

    if not ordered_uris:
        log("No playlisted tracks found to reorder.")
        return

    # Rebuild from persisted album state, but fetch the existing playlist so
    # the delete phase truly clears every item before the replacement is added.
    current_uris = get_playlist_track_uris(ctx, token, playlist_id, state)

    # Dev Mode apps can't PUT (replace) a playlist. Delete all current
    # tracks then POST them back in the desired order.
    log(f"Reordering {len(ordered_uris)} track(s) from {len(albums)} album(s)...")
    if current_uris:
        remove_tracks_from_playlist(ctx, token, playlist_id, current_uris, state)
    add_tracks_to_playlist(ctx, token, playlist_id, ordered_uris, state)
    log("Playlist reorder complete.")


def create_playlist(ctx, token, name, description=None):
    """Creates a private playlist for the authenticated user and returns
    its Spotify ID."""
    me = spotify_request(ctx, "GET", token, f"{ctx.spotify_api_base}/me", State())
    body = {"name": name, "public": False}
    if description:
        body["description"] = description
    resp = spotify_request(
        ctx, "POST", token, f"{ctx.spotify_api_base}/users/{me['id']}/playlists", State(),
        json_data=body)
    return resp["id"]


def apply_album_override(ctx, album_id, value):
    """Apply a manual include/exclude override for an album.

    Records ``manual_override`` in state, then syncs the playlist: excluding
    removes the album's tracks, re-including adds them back. Returns False
    if the album is unknown, True otherwise (even if the playlist sync
    failed -- the override itself is always persisted).
    """
    def _mutate(state):
        album = state.known_albums.get(album_id)
        if not album:
            return None
        if value == "true":
            album.manual_override = True
        elif value == "false":
            album.manual_override = False
        else:
            album.manual_override = None
        return state

    state = state_mod.update_state(ctx, _mutate)
    album = state.known_albums.get(album_id)
    if album is None:
        return False

    cfg = config_mod.load_config(ctx)
    playlist_id = cfg.get("spotify_playlist_id")
    if not playlist_id:
        return True

    client_id = cfg["spotify_client_id"]
    client_secret = cfg["spotify_client_secret"]
    refresh_token = auth_mod.load_refresh_token(ctx)
    if not all([client_id, client_secret, refresh_token]):
        return True

    try:
        token = auth_mod.get_access_token(ctx, client_id, client_secret, refresh_token)
        if value == "true" and album.added_to_playlist:
            track_uris = album.track_uris
            if not track_uris:
                track_uris = get_album_track_uris(ctx, token, album_id, state)
            if track_uris:
                remove_tracks_from_playlist(ctx, token, playlist_id, track_uris, state)
                log(f"Removed {len(track_uris)} track(s) from '{album.name}' (manually excluded)")

                def _mark_removed(s):
                    a = s.known_albums.get(album_id)
                    if a:
                        a.added_to_playlist = False
                        a.track_uris = []
                    return s
                state_mod.update_state(ctx, _mark_removed)
        elif value == "false":
            track_uris = get_album_track_uris(ctx, token, album_id, state)
            if track_uris:
                add_tracks_to_playlist(ctx, token, playlist_id, track_uris, state)
                log(f"Added {len(track_uris)} track(s) from '{album.name}' (re-included)")

                def _mark_added(s):
                    a = s.known_albums.get(album_id)
                    if a:
                        a.added_to_playlist = True
                        a.track_uris = list(track_uris)
                    return s
                state_mod.update_state(ctx, _mark_added)
    except Exception as e:
        log(f"WARNING: Override saved but playlist update failed: {e}")

    return True

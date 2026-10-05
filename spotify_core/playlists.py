"""Playlist operations: track sync, pruning, reordering, creation,
and the manual include/exclude override flow."""

from datetime import datetime

from . import auth as auth_mod
from . import config as config_mod
from . import state as state_mod
from .api import spotify_get, spotify_request
from .errors import RateLimitError
from .filters import (
    is_aged_out,
    is_effectively_excluded,
    is_past_retention,
    is_promoted,
    parse_release_date,
)
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
    limit, offset = 50, 0
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
    """Add tracks that are not already present in the target playlist.

    The playlist itself is the source of truth for deduplication. During a
    scan, the first add loads one playlist snapshot into ``state.in_progress``
    and every later add reuses and updates that snapshot. Because in-progress
    state is persisted, a rate-limit interruption can resume hours later
    without re-reading the playlist. The snapshot disappears when the scan
    clears ``in_progress`` at successful completion.
    """
    if not track_uris:
        return []

    in_progress = state.in_progress
    if in_progress is not None:
        if in_progress.playlist_track_uris is None:
            existing_uris = set(get_playlist_track_uris(ctx, token, playlist_id, state))
            in_progress.playlist_track_uris = list(existing_uris)
            state_mod.save_state(ctx, state)
            log(f"Loaded playlist {playlist_id} once for this scan ({len(existing_uris)} track(s)).")
        else:
            existing_uris = set(in_progress.playlist_track_uris)
    else:
        # Calls outside a scan (manual overrides, reorder, etc.) still read
        # Spotify directly so those operations always see the current playlist.
        existing_uris = set(get_playlist_track_uris(ctx, token, playlist_id, state))

    # Preserve the caller's order while also avoiding duplicate URIs in the
    # same add request.
    to_add = []
    seen = set()
    for uri in track_uris:
        if uri not in existing_uris and uri not in seen:
            to_add.append(uri)
            seen.add(uri)

    if not to_add:
        log(f"No new tracks to add to playlist {playlist_id}; all requested tracks already exist.")
        return []

    url = f"{ctx.spotify_api_base}/playlists/{playlist_id}/items"
    for i in range(0, len(to_add), 100):
        spotify_request(ctx, "POST", token, url, state, json_data={"uris": to_add[i:i + 100]})

    if in_progress is not None:
        # Keep the persisted snapshot authoritative for the rest of this scan.
        existing_uris.update(to_add)
        in_progress.playlist_track_uris = list(existing_uris)
        state_mod.save_state(ctx, state)
    return to_add


def remove_tracks_from_playlist(ctx, token, playlist_id, track_uris, state):
    url = f"{ctx.spotify_api_base}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        items = [{"uri": u} for u in track_uris[i:i + 100]]
        spotify_request(ctx, "DELETE", token, url, state, json_data={"items": items})


def _shared_track_guard(state, album_id):
    """URIs of every other album still in the playlist. Used so a removal
    never strips a track another album still needs."""
    return {
        uri
        for other_id, other in state.known_albums.items()
        if other_id != album_id and other.added_to_playlist
        for uri in (other.track_uris or [])
    }


def _album_track_uris(ctx, token, album_id, state):
    """Stored track URIs, falling back to a Spotify fetch. Returns None when
    the tracks can't be read, so callers skip the album this pass."""
    album = state.known_albums[album_id]
    if album.track_uris:
        return album.track_uris
    try:
        return get_album_track_uris(ctx, token, album_id, state)
    except RateLimitError:
        raise
    except Exception as e:
        log(f"  ERROR fetching tracks for '{album.name}': {e}")
        return None


def _retire_albums(ctx, token, state, days, playlist_id):
    """Drop albums whose retention window has closed: remove their tracks
    (keeping any a surviving album shares) and forget them entirely."""
    retire_ids = [
        album_id for album_id, album in state.known_albums.items()
        if is_past_retention(album, days)
    ]
    for album_id in retire_ids:
        album = state.known_albums[album_id]
        was_promoted = is_promoted(album)
        if album.added_to_playlist:
            track_uris = _album_track_uris(ctx, token, album_id, state)
            if track_uris is None:
                continue
            to_remove = [uri for uri in track_uris if uri not in _shared_track_guard(state, album_id)]
            if to_remove:
                try:
                    remove_tracks_from_playlist(ctx, token, playlist_id, to_remove, state)
                    log(f"  Removed {len(to_remove)} track(s) from retired '{album.name}'")
                except RateLimitError:
                    raise
                except Exception as e:
                    log(f"  ERROR removing retired '{album.name}' from playlist: {e}")
                    continue
        del state.known_albums[album_id]
        state_mod.save_state(ctx, state)
        if was_promoted:
            log(f"  Retired '{album.name}' -- retention window closed, "
                "even though it had been promoted.")
        else:
            log(f"  Retired '{album.name}' -- past its retention window.")


def _expire_albums(ctx, token, state, days, playlist_id):
    """Move aged-out albums into the Expired stage: remove their tracks but
    keep the entry so the dashboard can still list (and re-promote) it."""
    removal_ids = [
        album_id for album_id, album in state.known_albums.items()
        if album.added_to_playlist and is_aged_out(album, days)
    ]
    if not removal_ids:
        return

    keep_uris = {
        uri
        for album in state.known_albums.values()
        if album.added_to_playlist and not is_aged_out(album, days)
        for uri in (album.track_uris or [])
    }
    log(f"Expiring {len(removal_ids)} album(s) from the playlist (aged out or excluded)...")

    for album_id in removal_ids:
        album = state.known_albums[album_id]
        track_uris = _album_track_uris(ctx, token, album_id, state)
        if track_uris is None:
            continue
        to_remove = [uri for uri in track_uris if uri not in keep_uris]
        if to_remove:
            try:
                remove_tracks_from_playlist(ctx, token, playlist_id, to_remove, state)
                log(f"  Removed {len(to_remove)} track(s) from '{album.name}'")
            except RateLimitError:
                raise
            except Exception as e:
                log(f"  ERROR removing '{album.name}' from playlist: {e}")
                continue
        album.added_to_playlist = False
        album.track_uris = []
        state_mod.save_state(ctx, state)


def prune_playlist(ctx, token, state, days, playlist_id):
    """Retire albums past their retention window, then expire the ones that
    just aged out. RateLimitError propagates so the caller can abort."""
    if not playlist_id:
        return
    _retire_albums(ctx, token, state, days, playlist_id)
    _expire_albums(ctx, token, state, days, playlist_id)


def replace_playlist_contents(ctx, token, playlist_id, track_uris, state):
    """Clear the playlist with Spotify's replace endpoint, then rebuild it."""
    url = f"{ctx.spotify_api_base}/playlists/{playlist_id}/items"
    spotify_request(ctx, "PUT", token, url, state, json_data={"uris": []})
    for i in range(0, len(track_uris), 100):
        spotify_request(ctx, "POST", token, url, state, json_data={"uris": track_uris[i:i + 100]})


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
    seen_uris = set()
    for album in albums:
        for uri in album.track_uris or []:
            if uri not in seen_uris:
                ordered_uris.append(uri)
                seen_uris.add(uri)

    if not ordered_uris:
        log("No playlisted tracks found to reorder.")
        return

    log(f"Reordering {len(ordered_uris)} track(s) from {len(albums)} album(s)...")
    replace_playlist_contents(ctx, token, playlist_id, ordered_uris, state)
    log("Playlist reorder complete.")


def playlist_order_is_stale(ctx, token, state, playlist_id):
    """True if the playlist's current track order doesn't match the
    canonical release-date order reorder_playlist would produce.
    Read-only: one GET, no writes."""
    albums = [
        a for a in state.known_albums.values()
        if a.added_to_playlist and not is_effectively_excluded(a)
    ]

    def sort_key(album):
        parsed = parse_release_date(album.release_date)
        return parsed if parsed is not None else datetime.min

    current_uris = get_playlist_track_uris(ctx, token, playlist_id, state)
    known_uris = {uri for album in albums for uri in (album.track_uris or [])}
    observed_uris = [uri for uri in current_uris if uri in known_uris]
    seen_uris = set(observed_uris)

    expected_uris = []
    expected_seen = set()
    for album in sorted(albums, key=sort_key):
        album_uris = album.track_uris or []
        # Only albums represented in the current playlist participate in the
        # canonical comparison. Once an album is present, all of its known
        # tracks must be present too; this detects partial/missing albums while
        # still ignoring albums that have not been added to Spotify yet.
        if any(uri in seen_uris for uri in album_uris):
            for uri in album_uris:
                if uri not in expected_seen:
                    expected_uris.append(uri)
                    expected_seen.add(uri)

    return observed_uris != expected_uris


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



def apply_musicbrainz_override(ctx, release_group_id, value):
    """Set or clear a manual prerelease exclusion."""
    found = {"value": False}

    def _mutate(state):
        album = state.musicbrainz_upcoming.get(release_group_id)
        if not album:
            return None
        if value == "true":
            album.manual_excluded = True
        elif value == "false":
            album.manual_excluded = False
        else:
            return None
        found["value"] = True
        return state

    state_mod.update_state(ctx, _mutate)
    return found["value"]

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

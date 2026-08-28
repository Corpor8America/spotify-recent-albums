"""The scan pipeline: fetch followed artists, check due artists, record +
playlist-sync new albums, prune the playlist.

``run_scan`` is orchestration only; each phase lives in a focused helper
so steps can be tested independently.
"""

import threading
from datetime import datetime, timedelta, timezone

from .api import (
    ARTIST_ALBUMS_CATEGORY,
    DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    RateLimitError,
    blocked_until,
)
from .artists import get_artist_albums, get_due_artists, get_followed_artists
from .auth import get_access_token, load_refresh_token
from .config import CHECK_INTERVAL_DAYS, DEFAULT_DAYS_LOOKBACK, get_version, load_config, save_config
from .filters import is_auto_excluded, is_effectively_excluded, parse_release_date
from .logging import clear_logs, log
from .models import Album, Artist, MusicBrainzAlbum, ScanProgress
from .musicbrainz import get_artist_active, get_albums_in_window, get_albums_with_future_dates, resolve_spotify_to_mb
from .playlists import add_tracks_to_playlist, get_album_track_uris, playlist_order_is_stale, prune_playlist, reorder_playlist
from .state import clear_expired_rate_limits, load_state, save_state, update_state

# Serializes scan runs so the scheduler and a manual "Run now" click can
# never overlap.
run_lock = threading.Lock()

# Serializes playlist reorder runs.
reorder_lock = threading.Lock()

# Cancel event -- set by POST /cancel to abort a running scan.
_cancel_event = threading.Event()


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %I:%M:%S %p")


def cancel_scan(ctx):
    _cancel_event.set()
    # Also clear any in-progress state so a resume doesn't pick it up.
    def _clear_progress(state):
        state.in_progress = None
        return state
    update_state(ctx, _clear_progress)


def start_scan(ctx, days=None, interval_days=None, min_request_interval=None, market="US"):
    """Reserve the scan lock before starting its background thread."""
    if not run_lock.acquire(blocking=False):
        log("Scan already in progress -- skipping this trigger.")
        return False

    def _thread_main():
        try:
            run_scan(ctx, days=days, interval_days=interval_days,
                     min_request_interval=min_request_interval, market=market,
                     lock_held=True)
        except Exception as exc:
            # The thread must never die silently: clear any in-progress
            # markers (idempotent -- run_scan's finally usually did it
            # already) and surface the crash in the status log ring.
            def _clear(state):
                state.in_progress = None
                return state
            try:
                update_state(ctx, _clear)
            except Exception:
                pass
            log(f"Background scan crashed: {exc!r}")

    threading.Thread(target=_thread_main, daemon=True).start()
    return True


def record_album(state, artist, album, now_iso):
    """Insert/update an album in state, preserving existing override and
    playlist-sync fields."""
    existing = state.known_albums.get(album["id"])
    state.known_albums[album["id"]] = Album(
        id=album["id"],
        name=album["name"],
        artist=artist["name"],
        artist_id=artist["id"],
        album_type=album["album_type"],
        release_date=album["release_date"],
        url=album["external_urls"]["spotify"],
        total_tracks=album["total_tracks"],
        first_seen=existing.first_seen if existing else now_iso,
        auto_excluded=is_auto_excluded(album["name"]),
        manual_override=existing.manual_override if existing else None,
        added_to_playlist=existing.added_to_playlist if existing else False,
        track_uris=list(existing.track_uris) if existing else [],
    )


def run_scan(ctx, days=None, interval_days=None, min_request_interval=None, market="US", lock_held=False):
    """Runs one full scan pass. Safe to call from either the scheduler or a
    manual 'Run now' click -- run_lock ensures only one scan runs at a time."""
    if not lock_held and not run_lock.acquire(blocking=False):
        log("Scan already in progress -- skipping this trigger.")
        return {"status": "already_running"}

    clear_logs()
    _cancel_event.clear()

    try:
        cfg = load_config(ctx)
        days = days or cfg.get("days_lookback", DEFAULT_DAYS_LOOKBACK)
        interval_days = interval_days or cfg.get("interval_days", CHECK_INTERVAL_DAYS)
        ctx.rate_limiter.min_interval_seconds = (
            min_request_interval if min_request_interval is not None
            else cfg.get("min_request_interval", DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)
        )

        token = _authenticate(ctx, cfg)
        if token is None:
            return {"status": "not_connected"}

        state = load_state(ctx)
        if clear_expired_rate_limits(state):
            save_state(ctx, state)
        playlist_id = cfg["spotify_playlist_id"] or None
        blocked_categories = []

        # Phase 0: Process MusicBrainz upcoming releases releasing today
        _process_upcoming_releases(ctx, state)

        artists = _fetch_followed_artists(ctx, token, state, blocked_categories)
        used_priority = False
        any_new_albums = False
        if artists:
            plan = _plan_artists(
                ctx,
                state,
                artists,
                interval_days,
                blocked_categories,
                days,
                cfg.get("musicbrainz_priority_scan", False),
            )
            if plan is not None:
                due_artists, processed_ids, used_priority = plan
                any_new_albums = _process_artists(
                    ctx,
                    token,
                    state,
                    (due_artists, processed_ids),
                    days,
                    market,
                    playlist_id,
                    blocked_categories,
                )

        _finalize_progress(ctx, state, blocked_categories)
        _maybe_auto_disable_priority_scan(ctx, cfg, used_priority, blocked_categories, state)
        _prune_safely(ctx, token, state, days, playlist_id, blocked_categories)
        _maybe_auto_reorder(ctx, token, state, playlist_id, blocked_categories, any_new_albums)

        log("Scan finished." + (f" Blocked categories: {blocked_categories}" if blocked_categories else ""))
        return {"status": "ok", "blocked_categories": blocked_categories}
    finally:
        _cancel_event.clear()
        run_lock.release()


def _authenticate(ctx, cfg):
    client_id = cfg["spotify_client_id"]
    client_secret = cfg["spotify_client_secret"]
    refresh_token = load_refresh_token(ctx)
    if not client_id or not client_secret or not refresh_token:
        log("Not connected to Spotify yet -- visit /login first.")
        return None
    return get_access_token(ctx, client_id, client_secret, refresh_token)


def _process_upcoming_releases(ctx, state):
    """Check MusicBrainz upcoming albums for releases due today and remove them from the upcoming list."""
    today = datetime.now().strftime("%Y-%m-%d")
    to_remove = []
    for rg_id, album in state.musicbrainz_upcoming.items():
        if album.release_date <= today:
            to_remove.append(rg_id)
            log(f"MB: releasing today -- searching Spotify for '{album.name}' by {album.artist}")
    for rg_id in to_remove:
        del state.musicbrainz_upcoming[rg_id]
    if to_remove:
        save_state(ctx, state)


def _fetch_followed_artists(ctx, token, state, blocked_categories):
    try:
        log("Fetching followed artists...")
        artists = get_followed_artists(ctx, token, state)
        log(f"Found {len(artists)} followed artists.")
        return artists
    except RateLimitError as e:
        log(f"Skipping artist scan -- {e.category} rate-limited until {_fmt_ts(e.retry_until)}.")
        blocked_categories.append(e.category)
        return []


def _build_priority_order(ctx, state, artists, days_lookback):
    """Query MusicBrainz for artists with a release in the lookback
    window. Returns a list of artist_ids sorted by release date
    (oldest first). Artists that don't resolve or have nothing in the
    window are simply absent from the result -- callers must fall back
    to normal selection for everyone else."""
    candidates = []  # (release_date, artist_id)
    for artist in artists:
        artist_id = artist["id"]
        entry = state.artists.get(artist_id)
        mbid = entry.musicbrainz_id if entry else ""
        if not mbid:
            mbid = resolve_spotify_to_mb(artist_id)
            if not mbid:
                continue
            if entry is None:
                entry = Artist(id=artist_id, name=artist["name"])
                state.artists[artist_id] = entry
            entry.musicbrainz_id = mbid

        try:
            in_window = get_albums_in_window(ctx, mbid, days_lookback)
        except Exception as e:
            log(f"MB: priority lookup failed for {artist['name']}: {e}")
            continue

        for rg in in_window:
            release_date = parse_release_date(rg.get("first-release-date", ""))
            if release_date is not None:
                candidates.append((release_date, artist_id))

    candidates.sort(key=lambda c: c[0])
    seen = set()
    ordered_ids = []
    for _, artist_id in candidates:
        if artist_id not in seen:
            seen.add(artist_id)
            ordered_ids.append(artist_id)
    return ordered_ids


def _plan_artists(ctx, state, artists, interval_days, blocked_categories,
                  days_lookback, use_priority=False):
    """Resume an interrupted scan or start a fresh batch. Returns
    (due_artists, processed_ids, used_priority) or None when the album
    phase is blocked."""
    ip = state.in_progress
    if ip is not None:
        processed_ids = set(ip.processed_ids)
        due_artists = [a for a in artists if a["id"] in ip.due_ids]
        remaining_artists = [a for a in due_artists if a["id"] not in processed_ids]
        remaining = len(remaining_artists)
        log(f"Resuming: {remaining}/{len(due_artists)} remaining")
    else:
        due_artists = get_due_artists(artists, state, interval_days)
        processed_ids = set()
        remaining_artists = due_artists
        remaining = len(due_artists)
        state.in_progress = ScanProgress(due_ids=[a["id"] for a in due_artists], processed_ids=[])
        save_state(ctx, state)
        log(f"{len(due_artists)}/{len(artists)} artists due for a check (interval: {interval_days}d)")

    albums_blocked_until = blocked_until(state, ARTIST_ALBUMS_CATEGORY)
    if albums_blocked_until is not None:
        blocked_categories.append(ARTIST_ALBUMS_CATEGORY)
        log(f"Skipping album scan -- {ARTIST_ALBUMS_CATEGORY} rate-limited until "
            f"{_fmt_ts(albums_blocked_until)}. "
            f"{remaining} artist(s) will be checked on the next scan.")
        return None

    used_priority = False
    if use_priority and remaining_artists:
        priority_ids = _build_priority_order(ctx, state, remaining_artists, days_lookback)
        priority_set = set(priority_ids)
        by_id = {a["id"]: a for a in remaining_artists}
        prioritized_remaining = (
            [by_id[aid] for aid in priority_ids if aid in by_id]
            + [a for a in remaining_artists if a["id"] not in priority_set]
        )
        processed_prefix = [a for a in due_artists if a["id"] in processed_ids]
        due_artists = processed_prefix + prioritized_remaining
        state.in_progress.due_ids = [a["id"] for a in due_artists]
        save_state(ctx, state)
        used_priority = True
        log(f"MB: priority-ordered {len(remaining_artists)} pending artist(s).")

    return due_artists, processed_ids, used_priority


def _process_artists(ctx, token, state, plan, days, market, playlist_id, blocked_categories):
    due_artists, processed_ids = plan
    cfg = load_config(ctx)
    verbose = cfg.get("verbose_logging", False)
    mb_refresh_days = cfg.get("musicbrainz_active_refresh_days", 30)
    cutoff = datetime.now() - timedelta(days=days)
    now_iso = datetime.now(timezone.utc).isoformat()
    any_new_albums = False
    try:
        for i, artist in enumerate(due_artists, 1):
            if artist["id"] in processed_ids:
                continue
            if _cancel_event.is_set():
                log("  Scan cancelled by user.")
                break

            # --- MusicBrainz integration ---
            artist_entry = state.artists.get(artist["id"])
            if artist_entry is None:
                artist_entry = Artist(id=artist["id"], name=artist["name"])
                state.artists[artist["id"]] = artist_entry

            # Step 1: Resolve MusicBrainz ID if not cached
            if not artist_entry.musicbrainz_id:
                mbid = resolve_spotify_to_mb(artist["id"])
                if mbid:
                    artist_entry.musicbrainz_id = mbid
                    log(f"MB: resolved {artist['name']} -> {mbid}")
                else:
                    log(f"MB: no MusicBrainz mapping for {artist['name']}, skipping MB features")

            # Step 2: Check active status (only for artists currently marked active)
            if artist_entry.musicbrainz_id and artist_entry.mb_active:
                if not artist_entry.mb_active_checked:
                    # First time check
                    is_active = get_artist_active(artist_entry.musicbrainz_id)
                    artist_entry.mb_active = is_active
                    artist_entry.mb_active_checked = now_iso
                    log(f"MB: {artist['name']} is {'active' if is_active else 'inactive'}")
                else:
                    # Check if refresh is due
                    try:
                        last_checked_dt = datetime.fromisoformat(artist_entry.mb_active_checked)
                        days_since = (datetime.now(timezone.utc) - last_checked_dt).days
                        if days_since >= mb_refresh_days:
                            is_active = get_artist_active(artist_entry.musicbrainz_id)
                            artist_entry.mb_active = is_active
                            artist_entry.mb_active_checked = now_iso
                            log(f"MB: {artist['name']} is {'active' if is_active else 'inactive'}")
                    except (ValueError, TypeError):
                        pass

            # Step 3: Fetch MusicBrainz upcoming releases
            if artist_entry.musicbrainz_id:
                try:
                    upcoming = get_albums_with_future_dates(ctx, artist_entry.musicbrainz_id)
                    if upcoming:
                        log(f"MB: {len(upcoming)} upcoming album(s) for {artist['name']}")
                        for rg in upcoming:
                            rg_id = rg["id"]
                            if rg_id not in state.musicbrainz_upcoming:
                                state.musicbrainz_upcoming[rg_id] = MusicBrainzAlbum(
                                    id=rg_id,
                                    name=rg.get("title", ""),
                                    artist=artist["name"],
                                    artist_id=artist["id"],
                                    release_date=rg.get("first-release-date", ""),
                                    first_seen=now_iso,
                                )
                        save_state(ctx, state)
                except Exception as e:
                    log(f"MB: failed to fetch release groups for {artist['name']}: {e}")

            # Step 4: Skip logic
            # If artist has any MusicBrainz upcoming album with a future release date, skip Spotify check.
            has_upcoming = any(
                mb.artist_id == artist["id"] and mb.release_date > datetime.now().strftime("%Y-%m-%d")
                for mb in state.musicbrainz_upcoming.values()
            )
            if has_upcoming:
                if verbose:
                    log(f"  [{i}/{len(due_artists)}] {artist['name']} - skipping (upcoming MB release)")
                else:
                    log(f"  [{i}/{len(due_artists)}] {artist['name']} - skipped (upcoming MB release)")
                processed_ids.add(artist["id"])
                state.in_progress.processed_ids = list(processed_ids)
                save_state(ctx, state)
                continue

            # If inactive and no upcoming MB albums, skip.
            if not artist_entry.mb_active and not has_upcoming:
                if verbose:
                    log(f"  [{i}/{len(due_artists)}] {artist['name']} - skipping (inactive)")
                else:
                    log(f"  [{i}/{len(due_artists)}] {artist['name']} - skipped (inactive)")
                processed_ids.add(artist["id"])
                state.in_progress.processed_ids = list(processed_ids)
                save_state(ctx, state)
                continue

            # --- Normal Spotify check ---
            if verbose:
                log(f"  [{i}/{len(due_artists)}] {artist['name']} - fetching albums...")
            else:
                log(f"  [{i}/{len(due_artists)}] {artist['name']} - fetching albums...")
            try:
                albums = get_artist_albums(ctx, token, artist["id"], state, market)
            except RateLimitError:
                raise
            except Exception as e:
                log(f"    ERROR: {artist['name']}: {e}")
                processed_ids.add(artist["id"])
                state.in_progress.processed_ids = list(processed_ids)
                save_state(ctx, state)
                continue

            log(f"    Retrieved {len(albums)} album(s)")
            new_count = _record_new_albums(ctx, token, state, artist, albums, cutoff,
                                           playlist_id, now_iso)
            if new_count:
                log(f"    Added {new_count} new album(s)")
                any_new_albums = True
            else:
                log("    No new albums added")
            state.artists[artist["id"]] = Artist(
                id=artist["id"], name=artist["name"], last_checked=now_iso,
                scanned_with=get_version(ctx),
                musicbrainz_id=artist_entry.musicbrainz_id,
                mb_active=artist_entry.mb_active,
                mb_active_checked=artist_entry.mb_active_checked,
            )
            processed_ids.add(artist["id"])
            state.in_progress.processed_ids = list(processed_ids)
            save_state(ctx, state)
    except RateLimitError as e:
        log(f"Stopping scan -- {e.category} rate-limited until {_fmt_ts(e.retry_until)}. Progress saved.")
        blocked_categories.append(e.category)
    return any_new_albums


def _record_new_albums(ctx, token, state, artist, albums, cutoff, playlist_id, now_iso):
    new_count = 0
    for album in albums:
        if album["album_type"] != "album":
            continue
        artist_ids = [a["id"] for a in album.get("artists", [])]
        if artist["id"] not in artist_ids:
            continue
        release_date = parse_release_date(album["release_date"])
        if release_date and release_date < cutoff:
            continue

        is_unreleased = release_date and release_date.date() > datetime.now().date()

        existing_entry = state.known_albums.get(album["id"])
        needs_playlist_add = existing_entry is None or not existing_entry.added_to_playlist
        record_album(state, artist, album, now_iso)
        entry = state.known_albums[album["id"]]
        if needs_playlist_add and not is_effectively_excluded(entry) and playlist_id and not is_unreleased:
            try:
                track_uris = get_album_track_uris(ctx, token, album["id"], state)
                add_tracks_to_playlist(ctx, token, playlist_id, track_uris, state)
                entry.added_to_playlist = True
                entry.track_uris = track_uris
                log(f"      Added {len(track_uris)} track(s) from '{album['name']}'")
            except RateLimitError:
                raise
            except Exception as e:
                entry.added_to_playlist = False
                log(f"      ERROR adding '{album['name']}': {e}")
        new_count += 1
    return new_count


def _finalize_progress(ctx, state, blocked_categories):
    if not blocked_categories:
        state.in_progress = None
        save_state(ctx, state)


def _prune_safely(ctx, token, state, days, playlist_id, blocked_categories):
    try:
        prune_playlist(ctx, token, state, days, playlist_id)
    except RateLimitError as e:
        log(f"Skipping prune -- {e.category} rate-limited.")
        blocked_categories.append(e.category)


def _maybe_auto_disable_priority_scan(ctx, cfg, used_priority, blocked_categories, state):
    if not used_priority:
        return
    if blocked_categories or state.in_progress is not None:
        return
    if not cfg.get("musicbrainz_priority_scan", False):
        return

    cfg["musicbrainz_priority_scan"] = False
    save_config(ctx, cfg)
    log("MB priority scan completed cleanly; disabled MusicBrainz priority scan setting.")


def _maybe_auto_reorder(ctx, token, state, playlist_id, blocked_categories, any_new_albums):
    if not playlist_id or not any_new_albums or blocked_categories:
        return
    try:
        if playlist_order_is_stale(ctx, token, state, playlist_id):
            log("Playlist order drifted from release-date order -- reordering automatically.")
            reorder_playlist(ctx, token, state, playlist_id)
    except RateLimitError as e:
        log(f"Skipping auto-reorder -- {e.category} rate-limited.")

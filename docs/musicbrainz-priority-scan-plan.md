# MusicBrainz Priority Scan — Implementation Plan

## Problem

A cold start (or adding a large batch of new follows) means scanning all
followed artists' discographies via Spotify. With 117 artists, Spotify's
rate limits stretch this to ~4 days. `get_due_artists`' fallback batch
logic works through artists oldest-checked-first, blind to which ones
actually have anything new.

MusicBrainz has its own rate limit (1 req/sec), but querying release-groups
for 117 artists there takes minutes, not days.

## Goal

Use MusicBrainz as a **cheap pre-filter to prioritize and order** a large
backlog scan. MB never becomes the source of truth — Spotify's
`get_artist_albums` call remains authoritative for what gets recorded and
playlisted. MB only decides *which artists to check first* and *in what
order*, so that:

- Artists with a likely recent release surface (and hit the playlist)
  early, instead of waiting up to 4 days in rotation.
- Because albums get discovered roughly in chronological order as the
  scan progresses, the destructive `reorder_playlist` becomes an
  occasional manual cleanup rather than a step needed after every batch.
- Artists MB can't resolve, or has nothing for, fall through unchanged to
  today's behavior — bounding the blast radius of anything MB gets wrong.

This is additive and optional. No existing behavior for artists outside
the priority path changes.

---

## 1. New MusicBrainz function: windowed release-groups

**File:** `spotify_core/musicbrainz.py`

Add a sibling to the existing `get_albums_with_future_dates`, filtering to
the scan's lookback window instead of strictly future dates:

```python
def get_albums_in_window(ctx, mbid, days_lookback):
    """Get release-groups with first-release-date within
    [today - days_lookback, today], inclusive. Used to prioritize a large
    backlog scan; not a substitute for the Spotify-side date check."""
    cutoff = (datetime.now() - timedelta(days=days_lookback)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    release_groups = get_artist_release_groups(ctx, mbid)
    in_window = []
    for rg in release_groups:
        release_date = rg.get("first-release-date", "")
        if release_date and cutoff <= release_date <= today:
            in_window.append(rg)
    return in_window
```

Notes:
- Reuses `get_artist_release_groups` (already filters to `primary-type ==
  "Album"` and paginates), so no new HTTP surface — just a different date
  filter, matching the pattern of `get_albums_with_future_dates`.
- `first-release-date` can be year- or year-month-precision (MB, like
  Spotify, doesn't guarantee day precision). String comparison against
  `YYYY-MM-DD` cutoffs works for full dates; a `YYYY` or `YYYY-MM` value
  will sort correctly against the cutoff in most cases but not all edge
  cases at month/year boundaries. Reuse `parse_release_date` from
  `filters.py` instead of raw string comparison to avoid this — same
  Y/Y-m/Y-m-d handling Spotify dates already get.

Revised to use `parse_release_date`:

```python
from .filters import parse_release_date

def get_albums_in_window(ctx, mbid, days_lookback):
    cutoff = datetime.now() - timedelta(days=days_lookback)
    now = datetime.now()
    release_groups = get_artist_release_groups(ctx, mbid)
    in_window = []
    for rg in release_groups:
        parsed = parse_release_date(rg.get("first-release-date", ""))
        if parsed is not None and cutoff <= parsed <= now:
            in_window.append(rg)
    return in_window
```

---

## 2. Priority discovery pass

**File:** `spotify_core/scan.py` (new function, called from `run_scan`)

A new phase that runs *before* artist selection, resolving MB IDs (cached
in `Artist.musicbrainz_id`, same as today) and building a sorted candidate
list.

```python
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
            # persist resolution so _process_artists doesn't redo it later
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
    # De-dupe while preserving order (an artist can have multiple
    # in-window release-groups; only need the artist once, at its
    # earliest release date).
    seen = set()
    ordered_ids = []
    for _, artist_id in candidates:
        if artist_id not in seen:
            seen.add(artist_id)
            ordered_ids.append(artist_id)
    return ordered_ids
```

Save state once after the loop (MB ID resolutions persisted), same
pattern as `_process_artists` batches its saves.

---

## 3. Wiring into `run_scan` / artist selection

**File:** `spotify_core/scan.py`, `_plan_artists`

Only engage the priority pass on a **fresh** scan (no `state.in_progress`
resume in flight) and only for artists not already up to date — running
an MB query across 117 artists on every single scan is wasteful once the
backlog is cleared. Gate it behind either:

- A config flag (`musicbrainz_priority_scan: bool`, default `false`,
  settings-page toggle), explicitly opted into for a backlog catch-up, or
- An automatic heuristic: only run the priority pass when the number of
  due artists (`get_due_artists` result) exceeds some threshold (e.g. more
  than ~20% of followed artists), since that's the actual "big backlog"
  signal — a normal day-to-day scan with a handful of due artists doesn't
  need MB prioritization.

Recommend the config flag for the first version — simpler to reason about
and test, and it matches the project's existing pattern of explicit
settings rather than heuristics (see `interval_days`, `days_lookback`
being explicit knobs already). The threshold heuristic can be a follow-up
if the manual flag proves annoying to toggle.

```python
def _plan_artists(ctx, state, artists, interval_days, blocked_categories, days_lookback, use_priority):
    ip = state.in_progress
    if ip is not None:
        # unchanged: resume logic takes precedence, no priority pass on resume
        ...
    else:
        due_artists = get_due_artists(artists, state, interval_days)
        if use_priority and due_artists:
            priority_ids = _build_priority_order(ctx, state, due_artists, days_lookback)
            priority_set = set(priority_ids)
            # priority-ordered artists first, then the rest in their
            # existing due_artists order
            by_id = {a["id"]: a for a in due_artists}
            due_artists = (
                [by_id[aid] for aid in priority_ids if aid in by_id]
                + [a for a in due_artists if a["id"] not in priority_set]
            )
        ...
```

`run_scan` passes `cfg.get("musicbrainz_priority_scan", False)` down to
`_plan_artists` alongside the existing `days`/`interval_days` params.

This preserves every existing code path for `ip is not None` (resume) and
for `use_priority=False` (default) — zero behavior change unless someone
opts in.

---

## 4. Reorder implications

`reorder_playlist` itself doesn't need to change. But rather than leaving
reordering purely manual, add a drift check that auto-triggers it when
the playlist has actually fallen out of release-date order — priority
ordering should make this rare, so the check mostly confirms nothing's
needed, and quietly fixes it when something is (a seam artist, an
interrupted/resumed scan, a batch of catch-up albums added out of date
order within one artist).

### Drift detection

**File:** `spotify_core/playlists.py`

```python
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

    expected_order = [a.id for a in sorted(albums, key=sort_key)]

    uri_to_album = {}
    for a in albums:
        for uri in a.track_uris or []:
            uri_to_album[uri] = a.id

    current_uris = get_playlist_track_uris(ctx, token, playlist_id, state)
    observed_order = []
    seen = set()
    for uri in current_uris:
        album_id = uri_to_album.get(uri)
        if album_id and album_id not in seen:
            seen.add(album_id)
            observed_order.append(album_id)

    # Only compare albums both sides agree are currently playlisted --
    # tracks Spotify has that aren't in state (stray/manual additions)
    # don't count as drift.
    expected_filtered = [a for a in expected_order if a in seen]
    return observed_order != expected_filtered
```

This reuses `get_playlist_track_uris` (already exists) and the exact same
`sort_key` logic `reorder_playlist` uses, so "stale" is defined
consistently with what a reorder would actually produce -- no separate
tolerance/threshold concept to tune. It's strict (any album-order
mismatch counts as stale), which is fine because reorder should rarely
be needed after priority ordering ships; if it turns out to fire too
often in practice, a tolerance (e.g. allow N out-of-order album pairs)
can be added later without changing the function's contract.

### Wiring into `run_scan`

Check only when it's cheap to skip: only run the drift check if the
scan actually added at least one album this run (nothing added -> order
can't have changed -> skip the extra `GET` entirely), and only after a
scan phase that wasn't cut short by a rate limit (an incomplete scan's
playlist state is expected to be transiently "wrong" until it finishes).

```python
def _maybe_auto_reorder(ctx, token, state, playlist_id, blocked_categories, any_new_albums):
    if not playlist_id or not any_new_albums or blocked_categories:
        return
    try:
        if playlist_order_is_stale(ctx, token, state, playlist_id):
            log("Playlist order drifted from release-date order -- reordering automatically.")
            reorder_playlist(ctx, token, state, playlist_id)
    except RateLimitError as e:
        log(f"Skipping auto-reorder -- {e.category} rate-limited.")
```

Call this from `run_scan` right after `_prune_safely`, using the same
`token`/`state`/`playlist_id` already in scope. **Do not** route this
through `services.PlaylistService.reorder_async` / `reorder_lock` --
that path exists so a manual "Sort playlist" click can wait for a
scan to finish before starting (`reorder_lock` acquires `run_lock`
internally, see `services.py`). `run_scan` already holds `run_lock` for
its own entire duration, so calling through that path from inside a scan
would deadlock. Call `reorder_playlist` directly, the same way
`_prune_safely` already does.

### Cost trade-off

`reorder_playlist` is destructive (delete-all, re-add-all), so its API
cost scales with total playlist size, not with how out-of-order it is.
Auto-triggering it is only cheap in the sense that *detecting* staleness
is cheap (one extra GET) -- actually fixing it costs the same as a manual
reorder always has. This is fine given the whole premise of this plan is
that drift should become rare; if a particular library's playlist is
large enough that even occasional auto-reorders are unwelcome rate-limit
load, add a settings toggle (`auto_reorder_on_drift`, default `true`) to
opt out and fall back to fully manual reordering.

Worth a one-line note in `README.md` under "What's intentionally not
carried over" / usage docs: sorting the playlist is now largely
self-healing after a scan, rather than a step you need to remember to
run yourself.

---

## 5. State changes

**None new.** `Artist.musicbrainz_id` already exists and already gets
persisted via `Artist.to_dict()`/`from_dict()` (see
`spotify_core/models.py`). The priority pass reuses it — it just may
resolve/populate it earlier (during the priority pass) than it would have
otherwise (during `_process_artists`), which is fine since
`_process_artists`'s resolution step already checks
`if not artist_entry.musicbrainz_id` before calling
`resolve_spotify_to_mb` again.

One new config field:

```python
# spotify_core/config.py, default_config()
"musicbrainz_priority_scan": os.environ.get("MUSICBRAINZ_PRIORITY_SCAN", "false").lower() == "true",
```

Plus a settings-page checkbox (`templates/settings.html`, same pattern as
the existing `verbose_logging` checkbox) and form parsing in
`app.py`'s `settings()` POST handler.

---

## 6. Rate-limit / error handling

- MB failures during the priority pass must never abort the scan — catch
  and log per-artist (as shown in `_build_priority_order` above), same
  spirit as the existing `except Exception` around MB calls in
  `_process_artists`.
- The priority pass makes MB calls at the module's existing
  `_rate_limit()` pace (1 req/sec) — for 117 artists that's roughly two
  minutes of MB traffic before any Spotify call is made. No new rate
  limiter needed; reuse what's there.
- If a `RateLimitError` occurs on Spotify's side *during* the
  now-reordered artist loop, the existing resume/`blocked_categories`
  logic in `_process_artists`/`run_scan` is untouched — the only thing
  that changed is the *order* of `due_artists`, not the iteration or
  interruption handling.

---

## 7. Test coverage needed

Following the existing patterns in `tests/test_musicbrainz.py` (unit,
mocked `requests.get`), `tests/test_musicbrainz_scan.py` (scan-pipeline
integration with mocked MB functions), and `tests/test_scan.py`.

**`tests/test_musicbrainz.py` additions — `get_albums_in_window`:**
- Returns only release-groups within `[now - days_lookback, now]`.
- Excludes future dates and dates older than the window.
- Handles year/year-month precision dates via `parse_release_date`.
- Returns empty list on `None`/empty MB response (mirrors existing
  `get_albums_with_future_dates` tests).

**`tests/test_musicbrainz_scan.py` additions — `_build_priority_order`:**
- Resolves and persists MB IDs for artists that don't have one cached yet.
- Skips artists that don't resolve to an MBID (no crash, just excluded).
- Sorts candidates by release date ascending.
- De-dupes an artist appearing via multiple in-window release-groups.
- A per-artist MB lookup exception doesn't abort the whole pass (other
  artists still get processed).

**`tests/test_scan.py` additions — `_plan_artists` wiring:**
- `use_priority=False` (default): `due_artists` order is unchanged from
  today's `get_due_artists` output — regression guard.
- `use_priority=True` with priority hits: priority artists appear first,
  in release-date order; remaining due artists follow in their original
  order.
- `use_priority=True` but `state.in_progress` is set (resume in flight):
  priority pass is skipped entirely, resume logic behaves exactly as
  today (regression guard — this is the trickiest interaction to get
  wrong).
- `use_priority=True` with zero MB hits: falls through to identical
  behavior as `use_priority=False`.

**`tests/test_config.py` / `tests/test_app_routes.py` additions:**
- `musicbrainz_priority_scan` defaults to `False`.
- Settings POST round-trips the checkbox value, same as the existing
  `verbose_logging` test.

No new integration/mock-server tests should be strictly necessary —
`MockMusicBrainzServer` (`tests/mock_musicbrainz_server.py`) already
supports configurable per-artist release-groups and can back an
end-to-end test of a full `run_scan` with priority ordering enabled if
desired, but the unit-level coverage above should be sufficient to trust
the logic.

**`tests/test_playlists.py` additions — `playlist_order_is_stale`:**
- Returns `False` when the current playlist order already matches
  release-date order.
- Returns `True` when two albums' tracks are swapped relative to
  release-date order.
- Ignores playlist URIs that don't map back to a known album (stray/
  manual additions don't count as drift).
- Ignores albums in `expected_order` that aren't currently in the
  playlist (e.g. added but not yet synced) rather than treating their
  absence as drift.
- Excluded/pruned albums (`is_effectively_excluded`) are not part of
  either the expected or observed sequence.

**`tests/test_scan.py` additions — `_maybe_auto_reorder` wiring:**
- No new albums added this scan -> `playlist_order_is_stale` is never
  called (cost-gating regression guard).
- New albums added, order is stale -> `reorder_playlist` is called
  exactly once.
- New albums added, order is *not* stale -> `reorder_playlist` is not
  called.
- `blocked_categories` non-empty (scan was cut short by a rate limit) ->
  auto-reorder is skipped even if new albums were added.
- A `RateLimitError` from `playlist_order_is_stale`'s own `GET` is caught
  and logged, not raised out of `run_scan`.

---

## 8. Decisions

Resolved from the open questions raised during design review:

1. **Threshold vs. explicit flag:** explicit settings flag
   (`musicbrainz_priority_scan`) for v1. Revisit a heuristic
   (auto-engage above some due-artist percentage) later only if the
   manual flag proves annoying to toggle after a backlog clears.
2. **Cost of the priority pass if left permanently enabled:** accepted as
   a documentation problem, not a code problem — settings helper text
   should say this is meant for backlog catch-up, and the README should
   note it's worth disabling once the backlog clears. No auto-disable
   logic.
3. **MB/Spotify date-precision mismatches at the window boundary:**
   accepted as harmless — MB's window only affects ordering, never
   inclusion/exclusion, since Spotify's own date check in
   `_record_new_albums` remains the sole gate on what's recorded.
4. **Interrupted priority-ordered scans resuming across a multi-day
   lockout:** accepted as an edge case the automatic reorder (section 4)
   now cleans up after the fact, rather than something the scan itself
   needs to prevent.
5. **`_build_priority_order` persisting `mb_active` while it's already
   calling MB:** rejected as scope-creep — active-status checking stays
   exactly where it is today, in `_process_artists`.

## 9. Parked alternative: Apple Music as a data source

Considered and set aside during design review: MusicBrainz artist pages
can carry an "Apple Music" URL relation (`music.apple.com/{storefront}
/artist/{numeric_id}`), so an MB-resolved artist could in principle be
cross-referenced to an Apple Music catalog ID without fuzzy name
matching. Not pursued for this plan because:

- It doesn't replace MusicBrainz — it adds a hop *after* it (MB still
  needed to resolve the Apple ID), so it isn't actually a swap.
- It still requires standing up Apple Developer Program membership plus
  signed-JWT auth (ES256, key rotation) for a personal single-user
  project — real ongoing complexity for what would mainly be a
  day-precision date improvement that doesn't matter here, since MB's
  window only affects ordering, not inclusion.
- The Apple Music relation is itself optional/community-curated on MB,
  so its coverage is likely similar to (or worse than) the Spotify
  relation `resolve_spotify_to_mb` already depends on — it would face the
  same "some artists just won't resolve" fallback this plan already
  handles.

Worth revisiting only if MusicBrainz coverage turns out to be a real
practical bottleneck once the priority scan is running.

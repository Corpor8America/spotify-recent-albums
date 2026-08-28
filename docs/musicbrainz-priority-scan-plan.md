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

Engage the priority pass only when the album-scan category is not already
rate-limited, and only for artists that are actually still pending in the
current scan plan. Running an MB query across 117 artists on every single
scan is wasteful once the backlog is cleared. Gate it behind either:

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

Implementation requirements:

- Change `_plan_artists` to accept `days_lookback` and `use_priority`.
- Change `_plan_artists`'s successful return value from
  `(due_artists, processed_ids)` to
  `(due_artists, processed_ids, used_priority)`.
- `used_priority` must be `True` only when `_build_priority_order` was
  actually called. Do not set it just because the config flag is enabled.
- Check `blocked_until(state, ARTIST_ALBUMS_CATEGORY)` before doing any
  MusicBrainz priority work. If Spotify album scanning is already
  blocked, return `None` exactly as today and leave the setting enabled.
- On a fresh scan, build and persist `state.in_progress` before priority
  ordering so the reordered `due_ids` can be saved in the same field used
  by resume.
- On a resume, never remove or reprocess artists already listed in
  `state.in_progress.processed_ids`; only reorder the remaining artists.

Use this shape:

```python
def _plan_artists(ctx, state, artists, interval_days, blocked_categories,
                  days_lookback, use_priority):
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
        state.in_progress = ScanProgress(
            due_ids=[a["id"] for a in due_artists],
            processed_ids=[],
        )
        save_state(ctx, state)
        log(f"{len(due_artists)}/{len(artists)} artists due for a check "
            f"(interval: {interval_days}d)")

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
        # priority-ordered remaining artists first, then the rest in
        # their existing resume/fresh-plan order. Already-processed
        # artists stay recorded in processed_ids and are not rechecked.
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
```

`run_scan` passes `cfg.get("musicbrainz_priority_scan", False)` down to
`_plan_artists` alongside the existing `days`/`interval_days` params.
It must also unpack the new third return value and pass it to the
auto-disable helper:

```python
used_priority = False
...
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
        _process_artists(
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
```

This preserves every existing code path for `use_priority=False`
(default) — zero behavior change unless someone opts in. Resume behavior
does change when the setting is enabled: if a backlog is already in
`state.in_progress` because an earlier scan hit Spotify rate limits, the
priority pass reorders only the remaining, unprocessed artists. That lets
the setting help after the user turns it on mid-backlog instead of waiting
for the old unprioritized resume list to finish first.

After a priority-enabled scan completes the full planned artist list
without leaving `state.in_progress` set and without any
`blocked_categories`, automatically persist
`musicbrainz_priority_scan = false` back to the app config. This keeps the
priority pass as a one-time backlog catch-up tool instead of a permanent
two-minute MusicBrainz preflight someone has to remember to turn off.

Do **not** auto-disable when a scan is interrupted by Spotify rate limits,
crashes before cleanup, or exits with `state.in_progress` still populated.
Those cases mean the catch-up pass has not really finished yet. Also do
not auto-disable merely because `_build_priority_order` found zero MB
hits — the setting's job is to accelerate the whole backlog pass, and an
artist with no MB match still needs the normal Spotify scan to complete
before the backlog can be considered cleared.

```python
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
```

Add `save_config` to the existing config import in `spotify_core/scan.py`.
Call `_maybe_auto_disable_priority_scan` near the end of `run_scan`, after
`_finalize_progress` has had a chance to clear `state.in_progress`, but
before returning the final scan result. Keep `_prune_safely` after the
auto-disable call so a prune-side rate limit does not prevent disabling a
successfully completed priority catch-up. The priority pass only reorders
the album-scan work; pruning is a separate cleanup phase.

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

Because the setting auto-disables after a clean catch-up run, its helper
text should make that behavior explicit. For example: "Prioritize artists
with recent MusicBrainz releases during the next full catch-up scan; this
turns itself off after the scan completes."

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

### Implementation checklist

Do the implementation in this order:

1. Add `get_albums_in_window` to `spotify_core/musicbrainz.py` and export
   it from `spotify_core/__init__.py` if the package-level bindings need
   it for tests.
2. Import `get_albums_in_window` into `spotify_core/scan.py` and add
   `_build_priority_order`.
3. Import `save_config` into `spotify_core/scan.py`.
4. Update `_plan_artists` exactly as described in section 3:
   new params, new three-value return, Spotify blocked check before MB
   priority work, fresh-scan `state.in_progress` creation before priority
   ordering, resume-only reordering of unprocessed artists.
5. Update `run_scan` to initialize `used_priority = False`, pass the
   priority config into `_plan_artists`, unpack the three-value plan, and
   still pass only `(due_artists, processed_ids)` into `_process_artists`.
6. Add `_maybe_auto_disable_priority_scan` and call it immediately after
   `_finalize_progress`.
7. Add the config default, settings checkbox, settings POST parsing, and
   helper text.
8. Add tests before changing the implementation where practical, then run
   the focused test modules listed below.

Keep the auto-disable logic intentionally boring. Do not add thresholds,
percentages, counters, or "zero MB hits" heuristics. The only trigger is:
the priority pass actually ran, the scan is no longer blocked, and
`state.in_progress` has been cleared.

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
- Successful `_plan_artists` calls return three values:
  `(due_artists, processed_ids, used_priority)`.
- `use_priority=True` with priority hits: priority artists appear first,
  in release-date order; remaining due artists follow in their original
  order.
- `use_priority=True` with `state.in_progress` set (resume in flight):
  already-processed artists are not rechecked, but the remaining artists
  are priority-ordered and persisted back to `state.in_progress.due_ids`.
- `use_priority=True` with zero MB hits: falls through to identical
  artist ordering as `use_priority=False`, but `used_priority=True`
  because `_build_priority_order` did run.
- `use_priority=True` while `ARTIST_ALBUMS_CATEGORY` is already blocked:
  priority pass is skipped so the app does not spend MB calls before
  Spotify album scanning can resume, `_plan_artists` returns `None`, and
  `musicbrainz_priority_scan` remains enabled.

**`tests/test_scan.py` additions — `run_scan` wiring:**
- `run_scan` passes `days` as `days_lookback` into `_plan_artists`.
- `run_scan` passes `cfg["musicbrainz_priority_scan"]` into
  `_plan_artists`, defaulting to `False`.
- `_process_artists` still receives a two-item plan
  `(due_artists, processed_ids)`; do not make `_process_artists`
  understand `used_priority`.
- `_maybe_auto_disable_priority_scan` is called after
  `_finalize_progress`.

**`tests/test_scan.py` additions — auto-disable priority setting:**
- Fresh priority-enabled scan completes with no `blocked_categories` and
  no remaining `state.in_progress` -> config is written with
  `musicbrainz_priority_scan=False`.
- Priority-enabled scan is interrupted by a rate limit /
  `blocked_categories` -> setting remains enabled.
- Resume scan with `state.in_progress` already set and priority pass
  actually applied to remaining artists -> setting auto-disables after
  the resumed backlog completes cleanly.
- Resume scan with `state.in_progress` already set but album scanning is
  still rate-limited -> priority pass is skipped and setting remains
  enabled.
- Priority setting is already `False` -> no config write.

**`tests/test_config.py` / `tests/test_app_routes.py` additions:**
- `musicbrainz_priority_scan` defaults to `False`.
- Settings POST round-trips the checkbox value, same as the existing
  `verbose_logging` test.
- Settings page helper text mentions that the priority pass turns itself
  off after a clean catch-up scan.

No new integration/mock-server tests should be strictly necessary —
`MockMusicBrainzServer` (`tests/mock_musicbrainz_server.py`) already
supports configurable per-artist release-groups and can back an
end-to-end test of a full `run_scan` with priority ordering enabled if
desired, but the unit-level coverage above should be sufficient to trust
the logic.

Focused verification command:

```powershell
python -m unittest tests.test_musicbrainz tests.test_musicbrainz_scan tests.test_scan tests.test_config tests.test_app_routes
```

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
2. **Cost of the priority pass if left permanently enabled:** handle in
   code with narrow auto-disable logic. The setting remains explicit and
   opt-in, but after a priority-enabled scan completes cleanly
   (no `blocked_categories`, no remaining `state.in_progress`), `run_scan`
   writes `musicbrainz_priority_scan=false` back to config and logs it.
   Interrupted scans do not auto-disable the setting. Resume slices can
   auto-disable it only if the priority pass actually ran against the
   remaining artists and the resumed backlog then completed cleanly.
3. **MB/Spotify date-precision mismatches at the window boundary:**
   accepted as harmless — MB's window only affects ordering, never
   inclusion/exclusion, since Spotify's own date check in
   `_record_new_albums` remains the sole gate on what's recorded.
4. **Interrupted or pre-existing unprioritized scans resuming across a
   multi-day lockout:** handle this directly in `_plan_artists`. If the
   priority setting is enabled on a resume and Spotify album scanning is
   no longer blocked, priority-order only the remaining unprocessed
   artists and persist the updated `state.in_progress.due_ids`. Automatic
   reorder (section 4) still cleans up playlist drift after the fact.
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

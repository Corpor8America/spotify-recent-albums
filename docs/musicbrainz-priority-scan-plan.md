# MusicBrainz Pre-Scan Classifier — Implementation Notes

> This document describes the **shipped** design (replacing the earlier
> flag-based "priority scan" plan, which was deliberately simplified during
> implementation: the opt-in config flag, `_build_priority_order`, and the
> auto-disable heuristics were dropped in favor of an unconditional,
> self-contained pre-pass).

## Problem

A cold start (or adding a large batch of new follows) means scanning all
followed artists' discographies via Spotify. With 117 artists, Spotify's
rate limits stretch this to ~4 days. `get_due_artists`' fallback batch
logic works through artists oldest-checked-first, blind to to which ones
actually have anything new.

MusicBrainz has its own rate limit (1 req/sec), but querying release-groups
for 117 artists there takes minutes, not days.

## Goal

Use MusicBrainz as a **cheap pre-filter to prioritize and order** every scan
batch, so that:

- Artists MB says are inactive, or who only have future-dated albums, are
  skipped entirely — the Spotify API is *never* called for them, saving
  quota and wall-clock time on the day-to-day rotation.
- Artists with a likely recent release surface (and hit the playlist)
  early within a batch, instead of waiting out the whole rotation.
- Because albums get discovered roughly in chronological order as the
  scan progresses, the destructive `reorder_playlist` step fires rarely.
- Artists MB can't resolve or has nothing for fall through unchanged to
  today's behavior — bounding the blast radius of anything MB gets wrong.

MusicBrainz is **never the source of truth**. Spotify's
`get_artist_albums` call remains authoritative for what gets recorded and
playlisted. MB only decides *which artists to skip* and *in what order to
check the rest*.

---

## 1. Config / settings surface

The old design's two knobs were removed:

- `musicbrainz_priority_scan` (bool, settings checkbox) — the classifier
  now runs on **every** scan, no opt-in needed.
- `musicbrainz_active_refresh_days` (int) — replaced by a hardcoded
  `MB_ACTIVE_REFRESH_DAYS = 30` constant (see section 3).

So `spotify_core/config.py` gained nothing, the settings POST handler in
`app.py` no longer parses MB fields, and the MB group in
`templates/settings.html` is now header-only (no checkbox, no input).

## 2. New MusicBrainz function: status + release-groups in one call

**File:** `spotify_core/musicbrainz.py`

```python
MB_ACTIVE_REFRESH_DAYS = 30

def get_artist_status_and_release_groups(ctx, mbid):
    """Fetch an artist's active status and album release-groups in one call.

    Returns ``(active, release_groups)`` ... Assumes active / returns empty on
    error so a failure never blocks the scan.
    """
```

Gets `GET /ws/2/artist/{mbid}?inc=release-groups&limit=100&offset=N` in a
pagination loop, keeping only `primary-type == "Album"` release-groups.
`active` comes from `life_span.ended` on the first page. On any error it
returns `(True, [])`, so a transient MB failure degrades to "treat this
artist as normal and let Spotify decide".

The existing `get_artist_active`, `get_artist_release_groups`,
`get_albums_in_window`, `get_albums_with_future_dates`, and
`resolve_spotify_to_mb` helpers remain (used by the debug artist page,
reports, and upstream code).

## 3. Classification pass

**File:** `spotify_core/scan.py` — `_mb_classify_and_order(ctx, state,
artists, days_lookback, total_count, interval_days)`

Runs once per scan, inside `_plan_artists`, on the due batch *before* any
Spotify API call. Returns `(ordered_ids, skip_ids)`:

- `ordered_ids`: artist ids in processing order — "hits" first (artists
  with a release inside `[today - days_lookback, today]`, oldest release
  first), then everyone else in their original order. Skipped artists are
  included so the caller can mark them done without a Spotify call.
- `skip_ids`: artist ids that must never hit the Spotify API this run
  (inactive, or active with only future-dated release-groups).

Per artist, in order:

1. **Resolve MBID.** Use the cached `Artist.musicbrainz_id`, else
   `resolve_spotify_to_mb`; cache the result on `state.artists`. Unresolvable
   artists are NORMAL — no MB call, Spotify decides.
2. **Fresh inactive cache.** If `entry.mb_active == False` and
   `_active_check_is_fresh(entry)` (`mb_active_checked` parsed and newer
   than `MB_ACTIVE_REFRESH_DAYS` days), SKIP with zero MB calls.
3. **One MB call.** `get_artist_status_and_release_groups(ctx, mbid)`. On
   exception: log `MB: lookup failed for {name}: {e}` and treat as NORMAL.
   Cache `mb_active` / `mb_active_checked` (in ISO-8601 UTC) every time,
   including on the "still inactive" recheck path — that refresh is what
   prevents an MB call for the next 30 days.
4. **Inactive** → log `MB: {name} is inactive -- skipping`, SKIP.
5. **In-window hit.** Any album release-group with a parsed
   `first-release-date` in `[cutoff, now]` (using the same
   `parse_release_date` as Spotify dates) → HIT. Sort hits by earliest
   in-window date ascending (oldest first).
6. **Future-only.** No in-window releases but ≥ 1 release-group dated after
   today → log `MB: {name} has only upcoming releases -- skipping`, SKIP,
   and record the future release-groups into `state.musicbrainz_upcoming`
   (existing entries keep their `first_seen`; only new ids get a new
   `MusicBrainzAlbum`).
7. **Neither** → NORMAL. MB never excludes an artist it merely has no data
   for; Spotify stays authoritative.

### Early stop

`batch_worth = max(1, total_count // max(1, interval_days))` mirrors the
fallback batch logic: roughly how many artists can be *fully* scanned per
interval. If the due batch is larger than `batch_worth`, the MB loop stops
as soon as `len(hits_seen) >= batch_worth` — once enough hits are known,
the rest of the tail stays in its original order (trade-off: unclassified
tail artists may be skipped/reordered a day later than MB would have
allowed). On normal-size batches the whole due set is classified.

## 4. Skip semantics

Skipped artists are **kept inside the scan plan** (they appear in
`ordered_ids` in their original position and in `skip_ids`), so
`_process_artists` can walk the same list and:

1. Log `{name} - skipped (MB)` and add the id to `processed_ids` (so the
   scan terminates and resume stays clean), **without** a Spotify call, and
2. **Never update `last_checked`**, and
3. Stay inside `state.in_progress.due_ids`.

Because they're still due at the next scan, they're automatically
reclassified with current MB data. A future-only artist is skipped each
scan until its release date passes, at which point it becomes a HIT and is
checked normally — no stored date bookkeeping needed.

## 5. Upcoming-releases pruning

**File:** `spotify_core/scan.py` — `_prune_expired_upcoming(ctx, state)`

Called as scan `Phase 0`, before anything else. Removes every
`state.musicbrainz_upcoming` entry whose `release_date <= today`
(logging `MB: release date reached for '{name}' by {artist}`), and saves
state only when something was removed.

This is deliberately **prune-only**: the old code path that proactively
"searched Spotify for the release once it dropped" is gone. The artist is
still due (their `last_checked` was never updated by the classifier), so a
plain scan picks the album up via Spotify the same way it would any new
release.

## 6. Wiring into `_plan_artists` / `run_scan`

**File:** `spotify_core/scan.py`

```python
def _plan_artists(ctx, state, artists, interval_days, blocked_categories,
                  days_lookback, total_artist_count=None):
    """Resume an interrupted scan or start a fresh batch. Returns
    (due_artists, processed_ids, skip_ids) or None when the album
    phase is blocked."""
```

- `_plan_artists` takes no config flag — the classifier is unconditional.
- It passes `total_count = total_artist_count if given else len(artists)`
  into `_mb_classify_and_order` (so the early-stop `batch_worth` reflects
  the full followed-artist count, not just the due batch).
- The successful return value grew to a **three-item tuple**
  `(due_artists, processed_ids, skip_ids)`; on a resume it only reorders
  the remaining, unprocessed artists (a `processed_prefix` keeps done
  artists first).
- `_process_artists(ctx, token, state, plan, ...)` now unpacks all three
  values and honors `skip_ids`, but its per-artist MB resolution/status
  steps were removed — the classifier already persisted
  `musicbrainz_id`, `mb_active`, and `mb_active_checked`, and the artist
  rewrite in `_process_artists` preserves them from `state.artists`.

`run_scan` closes the loop:

```python
plan = _plan_artists(ctx, state, artists, interval_days,
                     blocked_categories, days,
                     total_artist_count=len(artists))
if plan is not None:
    due_artists, processed_ids, skip_ids = plan
    if skip_ids:
        log(f"MB: skipping {len(skip_ids)} artist(s) (inactive or future-only).")
    _process_artists(ctx, token, state,
                     (due_artists, processed_ids, skip_ids), ...)
```

If the classifier ordered any real (non-skipped) hits it logs
`MB: classified due batch; N artist(s) prioritized for Spotify.`

## 7. Rate-limit / error handling

- MB failures during the classification pass never abort the scan — caught
  and logged per artist, degrading to the normal Spotify path (same spirit
  as the `except Exception` around MB calls that used to live in
  `_process_artists`).
- `get_artist_status_and_release_groups` itself returns `(True, [])` on
  error, so even a hard MB outage just means "classify everything as
  normal".
- MB calls run at the module's existing `_rate_limit()` pace
  (~1.2-1.8 req/sec, headroom above MusicBrainz's hard 1/sec ceiling to
  avoid 503s when running on a shared Docker IP; see `_MIN_INTERVAL` /
  `_JITTER_SECONDS` in `musicbrainz.py`). The early-stop budget
  (section 3) bounds MB traffic on oversized batches.
- Each successful status/release-group call is logged per artist
  (`MB: {name} - active=..., N album release-group(s)`), the resulting
  classification is logged (`hit in window` / `is inactive` / `only
  upcoming releases`), and one summary line closes the pass
  (`MB: classified X/N artist(s) (Y newly resolved, Z skipped, W hit(s)).`)
  so the batch outcome is visible in the dashboard / docker logs.
- A `RateLimitError` from Spotify *during* the reordered loop behaves
  exactly as before: the run stops, `state.in_progress` (with the new
  ordering + `processed_ids` so far) is left in place, and resume picks up
  where it stopped — only the *order* of `due_artists` changed.

## 8. Auto-reorder drift check (unchanged)

Unrelated to MB but kept in `run_scan` for context: after a scan that added
albums and hit no rate limits, `_maybe_auto_reorder` checks
`playlist_order_is_stale` and, on drift, calls `reorder_playlist` directly
(never through `services.PlaylistService` / `reorder_lock`, which would
deadlock since `run_scan` already holds `run_lock`). It logs
`Playlist order drifted from release-date order -- reordering
automatically.` and swallows its own `RateLimitError`.

## 9. Test coverage

The classification semantics above live under `tests/test_scan.py`:

- `PlanArtistsTests` — 3-tuple return, order preserved without hits,
  hits reordered, blocked album phase → `None`, resume keeps processed
  artists and reorders the rest, skip propagation, classifier state
  persistence.
- `MbActiveCheckFreshTests` — fresh / stale / empty / garbage
  `mb_active_checked`.
- `MbClassifyAndOrderTests` — active-normal, inactive-skip+cache,
  fresh-inactive makes **no** MB call, stale-inactive rechecked
  (and re-checked again after refreshing — see "flips active"),
  hits oldest-first, future-only skip + upcoming recorded,
  `last_checked` untouched on skip, unresolvable → normal, lookup
  failure → normal, early-stop at `batch_worth` with tail preserved,
  hit+future recording, `musicbrainz_upcoming` `first_seen` preserved.

`tests/test_musicbrainz_scan.py` was reworked to patch
`get_artist_status_and_release_groups` instead of the older
status/window helpers:

- `PruneExpiredUpcomingTests` — today removed, past removed, future kept,
  empty no-op.
- `MbIdResolutionTests`, `MbActiveStatusTests`, `MbUpcomingReleasesTests`,
  `MbSkipLogicTests` — resolution caching, status caching, upcoming
  recording, skip ordering, failure fallbacks.

`tests/test_integration_mock.py`:

- `RunScanAgainstMockServerTests` / `AutoReorderIntegrationTests` fake the
  classifier with `_passthrough_classify` so non-MB scans never touch the
  real MusicBrainz network.
- `PriorityScanIntegrationTests` drives the real classifier against
  `MockMusicBrainzServer` (`/ws/2/artist/...` request log assert), the
  graceful MB-down path, and mid-scan rate-limit resume.

`tests/test_config.py` and `tests/test_app_routes.py` drop the removed
settings coverage; `tests/seed/app-config.json` no longer carries
`musicbrainz_active_refresh_days`.

Focused verification command:

```powershell
python -m unittest tests.test_scan tests.test_musicbrainz_scan tests.test_musicbrainz tests.test_config tests.test_app_routes tests.test_integration_mock
```

## 10. Decisions

Resolved against the earlier flag-based plan:

1. **Flag vs. always-on:** dropped `musicbrainz_priority_scan`. The MB
   pre-pass is unconditional. Cost is bounded by the early-stop budget;
   benefits (skipping dead/future-only artists) apply to every scan, not
   just backlog catch-ups. No auto-disable heuristics needed.
2. **One MB call per artist:** the classifier's per-artist work is a
   single paginated `get_artist_status_and_release_groups` call carrying
   both status and albums (vs. three separate helpers under the old
   design). A fresh inactive verdict short-circuits even that call.
3. **Skipped artists stay due:** skip never touches `last_checked` and
   keeps skipped ids in `state.in_progress.due_ids`, so a skipped artist
   is reclassified with current MB data on the next scan and flips to a
   HIT automatically the day its release drops.
4. **`musicbrainz_upcoming` is prune-only:** release-date-reached entries
   are dropped in Phase 0; discovery is left to the normal Spotify pass.
   The old "search Spotify when the date passes" trigger was removed.
5. **MB date precision at the window boundary:** accepted as harmless —
   MB's window only affects ordering/skip decisions, never inclusion,
   since Spotify's own date check in `_record_new_albums` remains the sole
   gate on what's recorded.
6. **Active status re-verified while classifying:** active artists get a
   fresh `mb_active_checked` every scan (free — it rides the same call the
   classifier already makes). Only *inactive* verdicts are cached for
   `MB_ACTIVE_REFRESH_DAYS` days to skip the call entirely.

## 11. Parked alternative: Apple Music as a data source

Considered and set aside during design review: MusicBrainz artist pages
can carry an "Apple Music" URL relation (`music.apple.com/{storefront}
/artist/{numeric_id}`), so an MB-resolved artist could in principle be
cross-referenced to an Apple Music catalog ID without fuzzy name
matching. Not pursued because:

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
  same "some artists just won't resolve" fallback this feature already
  handles.

Worth revisiting only if MusicBrainz coverage turns out to be a real
practical bottleneck once the classifier is running.
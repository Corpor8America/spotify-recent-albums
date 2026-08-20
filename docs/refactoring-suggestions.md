# Refactoring & Reorganization Suggestions

A collection of structural improvements to improve maintainability, reduce
duplication, and make the codebase easier to navigate as features continue to
grow.

---

## 1. Eliminate CLI/Core Duplication

**Problem:** `scripts/spotify-recent-albums.py` (792 lines) and `spotify_core.py`
(848 lines) share ~500 lines of near-identical code. Functions like
`spotify_request()`, `get_artist_albums()`, `prune_playlist()`,
`parse_release_date()`, `is_auto_excluded()`, `RateLimiter`, and
`LongRateLimitBlock` are duplicated verbatim. This means every bug fix or
feature change must be applied in two places.

**Suggestion:** Make the CLI script import from `spotify_core` instead of
re-declaring everything. `spotify_core` already has a clean public API that
the web app uses. The CLI script would become a thin wrapper:

```python
# scripts/spotify-recent-albums.py (after refactor)
import spotify_core as core

def main():
    # argparse, OAuth (local browser flow), markdown output
    # All core logic calls go through core.*
```

Functions unique to the CLI (e.g. `do_auth_flow()` with localhost HTTP server,
`format_markdown_table()`, `search_artist_albums()`) stay in the script. The
shared functions (`spotify_request`, `get_artist_albums`, `prune_playlist`,
etc.) come from `spotify_core`.

**Impact:** Eliminates ~500 lines, removes an entire class of "fixed in one
file but not the other" bugs, and means `test_spotify_recent_albums.py` can
drop its 17 duplicated test cases.

---

## 2. Break Up `spotify_core.py`

**Problem:** At 848 lines, `spotify_core.py` handles too many concerns:
configuration, token management, state persistence, rate limiting, Spotify
API calls, scan orchestration, playlist operations, and report queries.

**Suggestion:** Split into a package:

```
spotify_core/
    __init__.py          # re-exports public API for backward compatibility
    config.py            # load_config, save_config, is_configured, get_version
    state.py             # load_state, save_state, update_state, STATE_FILE
    auth.py              # get_auth_url, exchange_code_for_token, get_access_token,
                         #   save_refresh_token, load_refresh_token, is_connected
    api.py               # spotify_request, spotify_get, RateLimiter,
                         #   LongRateLimitBlock, endpoint_category, blocked_until
    artists.py           # get_followed_artists, get_due_artists, get_artist_albums
    playlists.py         # get_album_track_uris, get_playlist_track_uris,
                         #   add_tracks_to_playlist, remove_tracks_from_playlist,
                         #   create_playlist, prune_playlist, reorder_playlist
    scan.py              # run_scan, start_scan, cancel_scan, record_album
    reports.py           # get_report_albums, get_excluded_albums, get_upcoming_albums
    filters.py           # is_auto_excluded, is_effectively_excluded, parse_release_date
    logging.py           # log, get_recent_logs, clear_logs (ring buffer)
```

The `__init__.py` re-exports everything so `import spotify_core as core`
and all existing `core.func()` calls continue to work unchanged. This is a
**purely internal** reorganization -- no external behavior changes.

**Why a package, not just smaller files:** It lets you move things out
incrementally. Start by extracting one or two modules (e.g. `config.py` and
`state.py` since they have the fewest cross-dependencies), verify tests pass,
then continue.

---

## 3. Remove Module-Level Side Effects

**Problem:** Importing `spotify_core` has side effects:
- `DATA_DIR.mkdir(parents=True, exist_ok=True)` runs at import time (line 34),
  creating directories on disk.
- `rate_limiter = RateLimiter(...)` creates a global mutable singleton (line 215).

Similarly, importing `app.py` starts the APScheduler background thread
(`_start_scheduler()` at module level, line 503). Tests must set
`RUN_SCHEDULER=0` before importing `app.py`, which is fragile.

**Suggestion:**

- `DATA_DIR.mkdir()` should move into `run_scan()` or an explicit `init()`
  function called once at startup, not at import time.
- The `RateLimiter` instance should be created lazily or passed as a
  parameter, not as a module global.
- `_start_scheduler()` should be called from an `if __name__ == "__main__"`
  guard or a dedicated `create_app()` factory function.

**Flask app factory pattern:**

```python
# app.py
def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = cfg()["flask_secret_key"]
    _start_scheduler(app)
    return app

# At the bottom:
if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
```

This eliminates test fragility and makes it possible to create multiple app
instances for testing.

---

## 4. Extract Override Playlist Logic from Route Handler

**Problem:** The `set_override` route in `app.py` (lines 273-335) is 63 lines
with nested inner functions, multiple `core.update_state()` calls, and
conditional playlist add/remove logic. Route handlers should be thin.

**Suggestion:** Move the override logic into `spotify_core`:

```python
# spotify_core/playlists.py
def apply_album_override(token, state, playlist_id, album_id, value):
    """Apply a manual include/exclude override for an album.
    Handles playlist track add/remove and state updates.
    Returns a status message."""
    ...
```

The route handler becomes:

```python
@app.route("/albums/<album_id>/override", methods=["POST"])
def set_override(album_id):
    value = request.form.get("value")
    ...
    core.apply_album_override(token, state, playlist_id, album_id, value)
    return redirect(url_for("dashboard"))
```

---

## 5. Consolidate Version Handling

**Problem:** `version()` in `app.py` and `get_version()` in `spotify_core.py`
both read the VERSION file independently. `app.py` doesn't use `core.get_version()`.

**Suggestion:** Delete `version()` from `app.py` and use `core.get_version()`
everywhere. If the package split (suggestion 2) happens, `get_version()` lives
in `config.py`.

---

## 6. Add `__init__.py` to `tests/`

**Problem:** Test files manipulate `sys.path` to import source modules:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
```

This is brittle and makes test discovery order-dependent.

**Suggestion:** Add an empty `tests/__init__.py` and configure test imports
through `conftest.py` or `pyproject.toml` with proper package structure.
Alternatively, add a `pyproject.toml` at the root with:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

Then tests can do `import spotify_core` and `import app` directly without
`sys.path` hacks.

---

## 7. Adopt a `pyproject.toml`

**Problem:** The project uses `requirements.txt` with no tooling configuration.
There's no declared Python version, no linter config, no formatter config.

**Suggestion:** Add a `pyproject.toml`:

```toml
[project]
name = "spotify-recently-released-albums"
version = "1.5.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]

[tool.ruff]
line-length = 120
target-version = "py312"
```

This replaces `requirements.txt`, configures pytest, and adds a linter/formatter
in one place.

---

## 8. Separate Test Tiers

**Problem:** `test_spotify_core.py` mixes pure unit tests with integration-ish
tests that spin up a `MockSpotifyServer`. This makes the test suite slow and
makes it unclear which tests need the mock server running.

**Suggestion:** Split into:

```
tests/
    conftest.py                    # shared fixtures, DATA_DIR override
    test_filters.py                # is_auto_excluded, is_effectively_excluded, parse_release_date
    test_state.py                  # load_state, save_state, update_state, rate limits
    test_config.py                 # load_config, save_config, is_configured
    test_auth.py                   # OAuth URL generation, token exchange
    test_reports.py                # get_report_albums, get_excluded_albums, get_upcoming_albums
    test_api.py                    # RateLimiter, endpoint_category (unit only)
    test_scan.py                   # run_scan, get_due_artists, record_album (mocked API)
    test_playlists.py              # prune, reorder, add/remove tracks (mocked API)
    test_app_routes.py             # Flask route tests (test client)
    test_integration_mock.py       # Tests requiring MockSpotifyServer
    test_integration_docker.py     # Docker-based integration tests (unchanged)
```

Pure unit tests (`test_filters.py`, `test_state.py`) run instantly with no
mock server. Integration tests (`test_integration_mock.py`) clearly signal
they need infrastructure.

---

## 9. Clean Up the `scripts/` Directory

**Problem:** `scripts/add_missing_albums.py` uses a fragile `importlib` hack
to load the CLI script, then runs code at module level with no
`if __name__ == "__main__"` guard. The CLI script itself is being replaced
by the web app.

**Suggestion:**

- After the CLI-to-core deduplication (suggestion 1), `add_missing_albums.py`
  should import from `spotify_core` directly instead of dynamically importing
  the CLI script.
- Add `if __name__ == "__main__":` guards to all scripts.
- Consider whether `add_missing_albums.py` still needs to exist, or if the
  functionality it provides (adding tracks for specific album IDs) should be
  a one-off option in the web UI.

---

## 10. Template Organization

**Problem:** All templates live in a flat `templates/` directory. As pages grow
(flask-admin, API docs, debug tools), this becomes harder to navigate.

**Suggestion:** For now the flat structure is fine at 5 templates. When it
reaches ~8-10, consider:

```
templates/
    base.html
    dashboard/
        index.html
    artists/
        list.html
    settings/
        index.html
    debug/
        artist.html
```

Or use Flask's `blueprints` to group related routes and templates together.

---

## Priority Order

| # | Change | Effort | Impact | Risk |
|---|--------|--------|--------|------|
| 1 | CLI/Core deduplication | Medium | High | Low |
| 2 | Break up `spotify_core.py` | High | High | Medium |
| 3 | Remove module-level side effects | Low | Medium | Low |
| 4 | Extract override logic from route | Low | Low | Low |
| 5 | Consolidate version handling | Trivial | Low | None |
| 6 | Add `__init__.py` / fix test imports | Low | Medium | Low |
| 7 | Add `pyproject.toml` | Low | Medium | None |
| 8 | Separate test tiers | Medium | Medium | Low |
| 9 | Clean up `scripts/` | Low | Low | Low |
| 10 | Template organization | Low | Low | None |

**Recommended starting point:** Suggestions 1, 3, 5, and 7 are quick wins
that can be done in a single PR with high confidence. Suggestion 2 (package
split) is the biggest structural improvement but should be done incrementally
alongside suggestion 6 (test import cleanup).

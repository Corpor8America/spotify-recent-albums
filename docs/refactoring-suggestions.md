# Refactoring & Reorganization Suggestions

A collection of structural improvements to improve maintainability, reduce
duplication, and make the codebase easier to navigate as features continue to
grow.

---

## 1. ~~Eliminate CLI/Core Duplication~~ ✅ Done

The CLI script (`scripts/spotify-recent-albums.py`) has been removed along
with its tests and simulation harness. All duplicated code and the one-off
`add_missing_albums.py` script are gone.

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

## 9. ~~Clean Up the `scripts/` Directory~~ ✅ Done

The entire `scripts/` directory has been removed. The CLI script, one-off
utilities, and their tests are all deleted.

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

## 11. Dependency Injection Over Module Globals

**Problem:** Everything reads from module globals (`SPOTIFY_API_BASE`,
`rate_limiter`, `DATA_DIR`, `STATE_FILE`, `TOKEN_FILE`, `CONFIG_FILE`). The
package split (#2) helps organize code but doesn't fix the fundamental coupling.
Tests must monkey-patch globals to substitute dependencies:

```python
# test_spotify_core.py line 455-456
core.SPOTIFY_API_BASE = cls.server.base_url + "/v1"
core.rate_limiter.min_interval_seconds = 0
```

```python
# test_app.py lines 25-28
core.DATA_DIR = TEST_DIR
core.CONFIG_FILE = TEST_DIR / "app-config.json"
core.STATE_FILE = TEST_DIR / "spotify-state.json"
core.TOKEN_FILE = TEST_DIR / "spotify-token.json"
```

This is fragile: import order matters, tests can leak state into each other,
and it's impossible to run two independent instances in the same process.

**Suggestion:** Introduce an `AppContext` or `SpotifyClient` class that holds
all configurable state:

```python
@dataclass
class AppContext:
    data_dir: Path
    state_file: Path
    token_file: Path
    config_file: Path
    spotify_api_base: str
    rate_limiter: RateLimiter

    @classmethod
    def from_env(cls) -> "AppContext":
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        return cls(
            data_dir=data_dir,
            state_file=data_dir / "spotify-state.json",
            token_file=data_dir / "spotify-token.json",
            config_file=data_dir / "app-config.json",
            spotify_api_base=os.environ.get("SPOTIFY_API_BASE_OVERRIDE", "https://api.spotify.com/v1"),
            rate_limiter=RateLimiter(MAX_REQUESTS_PER_MINUTE, DEFAULT_MIN_REQUEST_INTERVAL_SECONDS),
        )
```

Functions receive `ctx` as a parameter instead of reading globals. The
`__init__.py` creates a default instance for backward compatibility:

```python
# spotify_core/__init__.py
_ctx = AppContext.from_env()

def load_state():
    return _state.load_state(_ctx)
```

This makes tests trivial: create a context pointing at a temp directory, no
monkey-patching needed.

---

## 12. Typed Data Models

**Problem:** State is a raw `dict` everywhere. Album entries are dicts with
12+ keys (`name`, `artist`, `artist_id`, `type`, `release_date`, `url`,
`total_tracks`, `first_seen`, `auto_excluded`, `manual_override`,
`added_to_playlist`, `track_uris`). There's no IDE completion, no type
checking, and typos in key names are silent bugs.

**Suggestion:** Dataclasses for the core domain objects:

```python
@dataclass
class Album:
    id: str
    name: str
    artist: str
    artist_id: str
    album_type: str
    release_date: str
    url: str
    total_tracks: int
    first_seen: str
    auto_excluded: bool = False
    manual_override: bool | None = None
    added_to_playlist: bool = False
    track_uris: list[str] = field(default_factory=list)

@dataclass
class Artist:
    id: str
    name: str
    last_checked: str = ""
    scanned_with: str = ""

@dataclass
class ScanProgress:
    due_ids: list[str]
    processed_ids: list[str]

@dataclass
class State:
    artists: dict[str, Artist]
    known_albums: dict[str, Album]
    in_progress: ScanProgress | None
    rate_limits: dict[str, int]
```

`load_state` / `save_state` serialize these to/from JSON. All callers work
with typed objects. A typo like `album["addded_to_playlist"]` becomes a
`AttributeError` at runtime and a red squiggle in the editor.

---

## 13. Storage Abstraction

**Problem:** State persistence is two ad-hoc JSON files with manual
`threading.RLock` / `tempfile.mkstemp` / `os.replace` dance. This is correct
but low-level. Swapping to SQLite (for concurrent reads, migrations, or
querying) would require rewriting every caller.

**Suggestion:** Define an `AbstractStore` interface:

```python
class Store(Protocol):
    def load_state(self) -> State: ...
    def save_state(self, state: State) -> None: ...
    def load_config(self) -> dict: ...
    def save_config(self, config: dict) -> None: ...
    def load_refresh_token(self) -> str | None: ...
    def save_refresh_token(self, token: str) -> None: ...
```

Implement `JsonFileStore` for the current behavior. Later, add `SqliteStore`
without changing any business logic. The store is injected via `AppContext`.

---

## 14. Error Taxonomy

**Problem:** The codebase uses a mix of `LongRateLimitBlock` (custom),
`RuntimeError` (built-in), and generic `Exception`. Error handling in
`run_scan` catches `LongRateLimitBlock` separately but everything else falls
into a broad `except Exception`. Route handlers catch `Exception` and return
500 with the error message inline.

**Suggestion:** A small hierarchy:

```python
class SpotifyCoreError(Exception): ...

class RateLimitError(SpotifyCoreError):
    category: str
    retry_until: int

class SpotifyAPIError(SpotifyCoreError):
    status_code: int
    message: str

class ConfigError(SpotifyCoreError): ...
class AuthError(SpotifyCoreError): ...
```

Handlers can `except RateLimitError` precisely, and route handlers can map
error types to HTTP status codes cleanly.

---

## 15. Break Up `run_scan` God Function

**Problem:** `run_scan()` (lines 700-848, ~150 lines) handles token refresh,
artist fetching, due-artist selection, album filtering, album recording,
playlist track add, pruning, error recovery, progress tracking, and
cancellation -- all in one function with deep nesting.

**Suggestion:** Decompose into a pipeline of smaller functions:

```python
def run_scan(...):
    ctx = _prepare_scan_context(days, interval_days, min_request_interval)
    if not ctx:
        return ctx.error_result

    artists = _fetch_followed_artists(ctx)
    due_artists = _select_due_artists(ctx, artists)
    _process_albums(ctx, due_artists)
    _finalize_scan(ctx)
```

Each step is independently testable with mocked dependencies. The orchestration
logic stays in `run_scan` but the implementation details are in focused
functions.

---

## 16. Service Layer Between Routes and Core

**Problem:** `app.py` route handlers directly call `core.*` functions and
handle token refresh, error mapping, and redirect logic inline. The
`set_override` handler (63 lines) and `_do_reorder` (25 lines) are
particularly complex. There's no layer between HTTP and business logic.

**Suggestion:** Introduce lightweight service objects:

```python
# app/services/scan_service.py
class ScanService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def trigger_scan(self) -> bool:
        """Start a background scan. Returns True if started."""
        ...

    def get_status(self) -> dict:
        """Return current scan/queue status for the dashboard."""
        ...

# app/services/playlist_service.py
class PlaylistService:
    def apply_override(self, album_id: str, value: str) -> None: ...
    def reorder(self) -> None: ...
    def create(self, name: str) -> str: ...
```

Routes become thin dispatchers:

```python
@app.route("/albums/<album_id>/override", methods=["POST"])
def set_override(album_id):
    svc = PlaylistService(ctx)
    svc.apply_override(album_id, request.form.get("value"))
    return redirect(url_for("dashboard"))
```

---

## 17. Structured Logging

**Problem:** The ring buffer (`_log_lines`) is a flat list of strings with
timestamps prepended by `log()`. There's no structured logging (JSON format),
no log levels, and no way to filter by severity. For a containerized service
this makes debugging harder.

**Suggestion:** Use Python's `logging` module with a JSON formatter:

```python
import logging

logger = logging.getLogger("spotify_core")

class RingBufferHandler(logging.Handler):
    """Feeds into the existing ring buffer for the dashboard."""
    ...
```

The ring buffer becomes a `logging.Handler` instead of a custom implementation.
Structured fields (artist_id, album_id, category) can be attached as
`extra={}` for filtering. Console output goes through the standard logger
(JSON in containers, human-readable locally).

---

## 18. Health Check Endpoint

**Problem:** The only health signal is `GET /status` which does a full state
file read. There's no lightweight liveness probe for Docker/Kubernetes.

**Suggestion:** Add `GET /healthz`:

```python
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True}), 200
```

For a deeper readiness probe:

```python
@app.route("/readyz")
def readyz():
    cfg = core.load_config()
    ready = bool(cfg.get("spotify_client_id"))
    return jsonify({"ready": ready}), 200 if ready else 503
```

---

## Priority Order

| # | Change | Effort | Impact | Risk |
|---|--------|--------|--------|------|
| 1 | ~~CLI/Core deduplication~~ | ~~Medium~~ | ~~High~~ | ~~Low~~ |
| 2 | Break up `spotify_core.py` | High | High | Medium |
| 3 | Remove module-level side effects | Low | Medium | Low |
| 4 | Extract override logic from route | Low | Low | Low |
| 5 | Consolidate version handling | Trivial | Low | None |
| 6 | Add `__init__.py` / fix test imports | Low | Medium | Low |
| 7 | Add `pyproject.toml` | Low | Medium | None |
| 8 | Separate test tiers | Medium | Medium | Low |
| 9 | ~~Clean up `scripts/`~~ | ~~Low~~ | ~~Low~~ | ~~Low~~ |
| 10 | Template organization | Low | Low | None |
| 11 | Dependency injection | Medium | High | Medium |
| 12 | Typed data models | Medium | High | Low |
| 13 | Storage abstraction | Medium | Medium | Low |
| 14 | Error taxonomy | Low | Medium | Low |
| 15 | Break up `run_scan` | Medium | High | Low |
| 16 | Service layer | Medium | Medium | Low |
| 17 | Structured logging | Low | Medium | Low |
| 18 | Health check endpoints | Trivial | Low | None |

**Recommended order:**

1. **Quick wins first (single PR):** 3, 5, 7, 18 -- remove side effects,
   consolidate version, add pyproject.toml, add health check. Zero risk.
2. **Test infrastructure (second PR):** 6, 8 -- fix test imports, split test
   tiers. Low risk, makes后续 work safer.
3. **Package split (incremental PRs):** 2 -- extract one module at a time,
   verify tests after each.
4. **Type safety (after package split):** 12, 14 -- add dataclasses and error
   hierarchy. These touch many files but are mechanical.
5. **Architecture (after type safety):** 11, 13, 15, 16 -- DI, storage
   abstraction, scan decomposition, service layer. These build on the typed
   models.
6. **Polish:** 10, 17 -- template organization and structured logging.

---

## Test Coverage Analysis

### What the tests cover today

**Unit tests (`test_spotify_core.py`, 694 lines):**
- Filters: `is_auto_excluded`, `is_effectively_excluded`, `parse_release_date`
- State: `load_state`, `save_state`, `clear_expired_rate_limits`
- Config: `load_config`, `save_config`, `is_configured`
- Token: `save_refresh_token`, `load_refresh_token`, `is_connected`
- Reports: `get_report_albums`, `get_excluded_albums`, `get_upcoming_albums`
- Scan helpers: `get_due_artists`, `record_album`
- Rate limiting: `RateLimiter`, `endpoint_category`, `blocked_until`
- Playlist: `prune_playlist`, `reorder_playlist`
- Logging: `log`, `get_recent_logs`, `clear_logs`
- Cancel: `cancel_scan`
- Integration-ish: `create_playlist` and `run_scan` (blocked category) against MockSpotifyServer

**Route tests (`test_app.py`, 308 lines):**
- Dashboard: renders, redirects when unconfigured, shows connected/not-connected, upcoming albums section, rate limit banner
- Settings: renders, POST saves, create playlist button visibility
- OAuth: login redirect, callback missing code, callback with error
- Scan: run now triggers scan, cancel calls cancel
- Overrides: set true/false, unknown album 404
- Status: JSON shape, connected true/false

**Docker integration tests (`test_integration_docker.py`, 525 lines):**
- App readiness and `/status` shape
- Dashboard renders
- Settings renders and POST saves
- OAuth full flow (login → authorize → callback → connected)
- Create playlist creates and saves ID
- Run now redirects and scan completes
- Cancel scan redirects
- Reorder redirects
- Full scan discovers albums and syncs tracks to playlist
- Second scan does not duplicate tracks
- Reorder sorts by release date
- Excluded album pruned on rescan
- Re-included album re-added on rescan
- Auto-excluded (parenthetical) album not playlisted
- Rate limit lockout stops scan
- Data persists across container restart
- Override unknown album 404
- Override known album redirects

### Coverage gaps

| Feature | Unit | Route | Docker E2E | Risk |
|---------|------|-------|------------|------|
| `GET /artists` (artists list page) | - | - | - | **HIGH** -- untested route |
| `GET /debug/artist` (empty form) | - | - | - | **MEDIUM** |
| `POST /debug/artist` (valid/invalid input) | - | - | - | **MEDIUM** |
| `get_auth_url()` | - |间接 | 间接 | Low |
| `get_version()` | - | - | - | Low |
| `update_state()` | 间接 (override) | 间接 | 间接 | Low |
| `format_rate_limit_until()` | - | 间接 | - | Low |
| `_format_last_checked()` | - | - | - | Low |
| Settings validation (bad cron, bad playlist ID) | - | - | - | **MEDIUM** -- no negative tests |
| Scan resume after interruption | - | - | - | **HIGH** -- critical path, no test |
| Concurrent scan + reorder | - | - | - | **MEDIUM** |
| Multiple sequential scans with state changes | - | - | 间接 | Low |
| Reorder with 3+ albums (ordering correctness) | 间接 | - | 2 artists only | **MEDIUM** |
| Override include flow (add tracks back) | - | - | ✅ (re-included) | Low |
| Override exclude flow (remove tracks) | - | - | ✅ (excluded) | Low |
| `spotify_request` retry on 502/503 | - | - | - | Low |
| `spotify_request` non-retryable error | - | - | - | Low |
| Token refresh failure | - | - | - | Low |
| State file corruption recovery | - | - | - | Low |

### E2E tests needed before refactoring

These tests should be added **before** any structural changes to lock down
behavior that the refactor must preserve:

#### 1. `GET /artists` page (no tests at all)

```python
def test_artists_page_renders(self):
    """The /artists route must render the artists list page."""

def test_artists_page_shows_empty_state(self):
    """When no artists are tracked, /artists shows an empty message."""

def test_artists_page_shows_tracked_artists(self):
    """After a scan, /artists lists each artist with last-checked time."""

def test_artists_page_shows_scan_status_during_scan(self):
    """During an in-progress scan, /artists shows pending/done per artist."""
```

#### 2. `/debug/artist` route (no tests at all)

```python
def test_debug_artist_empty_form(self):
    """GET /debug/artist renders the empty form."""

def test_debug_artist_invalid_input(self):
    """POST /debug/artist with garbage input returns an error message."""

def test_debug_artist_valid_id(self):
    """POST /debug/artist with a valid 22-char ID returns album data."""

def test_debug_artist_valid_url(self):
    """POST /debug/artist with a Spotify URL extracts the ID and works."""
```

#### 3. Settings validation (no negative tests)

```python
def test_settings_rejects_invalid_cron(self):
    """POST /settings with an invalid cron schedule returns 400."""

def test_settings_rejects_invalid_playlist_id(self):
    """POST /settings with a non-alphanumeric playlist ID returns 400."""

def test_settings_rejects_negative_interval(self):
    """POST /settings with interval_days < 1 returns 400."""
```

#### 4. Scan resume after interruption

```python
def test_scan_resumes_from_last_processed_artist(self):
    """If a scan is interrupted (rate limit), the next scan resumes
    from where it left off, not from the beginning."""

def test_scan_in_progress_state_cleared_on_completion(self):
    """After a scan completes normally, in_progress is set to None."""
```

#### 5. Reorder ordering correctness

```python
def test_reorder_with_three_albums_sorts_oldest_first(self):
    """Reorder must produce oldest-album-first ordering with 3+ albums."""

def test_reorder_preserves_track_count(self):
    """After reorder, total tracks in playlist equals sum of all
    album track_uris (no duplicates, no losses)."""
```

#### 6. Error / edge cases

```python
def test_run_scan_not_connected(self):
    """run_scan returns not_connected when no refresh token exists."""

def test_run_scan_no_config(self):
    """run_scan returns not_connected when client_id is empty."""

def test_override_does_not_add_unreleased_album(self):
    """Overriding an unreleased album to include must not add it
    to the playlist until its release date passes."""

def test_prune_does_not_remove_shared_tracks(self):
    """When two albums share a track URI, pruning one must not
    remove the shared track if the other still needs it."""
```

### Test infrastructure improvements

Before adding the tests above, fix the test infrastructure:

1. **Add `tests/__init__.py`** and a `conftest.py` (or `pyproject.toml`
   `[tool.pytest.ini_options]` with `pythonpath = ["."]`) so tests don't
   need `sys.path` hacks.

2. **Add a `conftest.py`** with a shared fixture that creates a temp
   `DATA_DIR` and tears it down after each test, eliminating the fragile
   module-level `os.environ` mutations:

   ```python
   @pytest.fixture(autouse=True)
   def temp_data_dir(tmp_path):
       os.environ["DATA_DIR"] = str(tmp_path)
       os.environ["RUN_SCHEDULER"] = "0"
       # Reset module-level state
       import spotify_core as core
       core.DATA_DIR = tmp_path
       core.STATE_FILE = tmp_path / "spotify-state.json"
       core.TOKEN_FILE = tmp_path / "spotify-token.json"
       core.CONFIG_FILE = tmp_path / "app-config.json"
       yield
   ```

3. **Create a Flask test fixture** for route tests:

   ```python
   @pytest.fixture
   def client(temp_data_dir):
       from app import create_app
       app = create_app()
       app.config["TESTING"] = True
       with app.test_client() as c:
           yield c
   ```

   (Requires implementing the `create_app` factory from suggestion 3.)

4. **Separate mock-server tests** from pure unit tests so `pytest` can
   run the fast suite in seconds without starting the mock server.

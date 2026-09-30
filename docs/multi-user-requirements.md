# Multi-user conversion: requirements and implementation plan

**Status:** Planning document; no application behavior is changed by this document.  
**Branch:** `docs/multi-user-requirements`  
**Scope:** Convert the current single-user Spotify web application into a multi-user application, including application-managed authentication while retaining Cloudflare Tunnel for connectivity.

## 1. Purpose and problem statement

The application currently behaves as one shared Spotify account and one shared installation. Its configuration, Spotify refresh token, scan state, logs, playlist target, and background work are process-wide or stored in one shared data directory. This is appropriate for a single operator, but it is not a safe multi-user boundary.

The application is hosted behind a CGNAT, so Cloudflare Tunnel is required to make it reachable. Cloudflare's requirement is connectivity only; Cloudflare Access and its Google/whitelisted-email authentication flow are not requirements. Multi-user authentication and authorization must be implemented and enforced by the application.

The goal is to let multiple independent users access one deployed application while each user connects their own Spotify account and has private configuration, scan state, album decisions, logs, and playlist settings.

This document describes the required behavior, architecture changes, migration, security requirements, implementation sequence, and acceptance tests. It is not authorization to implement every optional feature; product decisions called out below should be settled before implementation.

## 2. Current application behavior and constraints

The current repository is a Flask application with a service layer and a `spotify_core` package. The app is launched through `wsgi.py`; scheduled scans are managed in-process with APScheduler.

Relevant current design characteristics:

- `app.py` creates Flask routes and currently reads one configuration and one state.
- `spotify_core/context.py` creates a process-wide default `AppContext`, backed by a `JsonFileStore` under `DATA_DIR` (normally `/data`).
- The store persists one `app-config.json`, `spotify-state.json`, and `spotify-token.json`.
- `spotify_core/config.py` merges one saved configuration with defaults and persists a Flask session secret in that same configuration.
- `spotify_core/auth.py` implements Spotify OAuth and refresh-token exchange. The refresh token is persisted in the shared store.
- `spotify_core/scan.py` and the exported core API use module-level scan/reorder locks and cancellation state.
- `services.py` orchestrates scans and playlist operations using the default core context.
- Dashboard, settings, artist list, debug tools, and album override routes currently operate on that one shared state.
- The current OAuth callback stores the Spotify refresh token for the whole installation. OAuth state is held in the Flask session.
- The current deployment uses Caddy as a reverse proxy. Cloudflare Access is an external access layer, not an application-level user database.

The existing `AppContext` and `Store` abstraction are useful seams, but they do not by themselves provide tenant isolation. A context must be selected from the authenticated user for every request and every background task; a process-global context cannot safely represent concurrent users.

## 3. Product requirements

### 3.1 User identity and sign-in

1. The application must authenticate users itself. Cloudflare Tunnel remains in the network path to provide connectivity through the CGNAT; Cloudflare Access must not be a prerequisite.
2. The application must authenticate each user before showing private application data or accepting mutations.
3. A user must have a stable internal user ID that is not derived from a mutable display name.
4. Sign-in and sign-out must be supported from the app.
5. Sessions must expire and be revocable. Signing out must invalidate the current session.
6. Authentication must protect against CSRF, session fixation, replay of OAuth state, and open redirects.
7. The app must provide clear behavior for an unauthenticated visitor: show the sign-in page or redirect to it, rather than returning another user's dashboard.
8. Account recovery, invitation, registration, and administrative user management require an explicit product decision (see Section 12).

**Identity-provider decision:** The application may implement its own email-based login, use a supported non-Google OIDC provider, or support both. Do not assume that simply removing Cloudflare Access creates secure authentication. The selected flow must be documented, tested, and operable in the target deployment. If using email/password, requirements include password hashing with a modern password hash, rate limiting, reset flow, and secure email delivery. If using OIDC, validate issuer, audience, signature, nonce, state, expiry, and redirect URI.

### 3.2 User data isolation

Each user must have independent:

- Spotify Client ID/Client Secret configuration, unless a deliberate shared application-level Spotify client is chosen.
- Spotify authorization and refresh token.
- Spotify playlist ID and playlist creation settings.
- Scan interval, cron schedule, lookback window, request pacing, and verbose logging preference.
- Artist tracking state, including last checked, MusicBrainz linkage/status, and scan progress.
- Known album state, manual include/exclude overrides, playlist-added markers, and track URIs.
- Rate-limit state and scan/reorder status.
- Application logs and user-visible operational messages.
- Any future user-specific preferences.

A user must never be able to read, modify, or trigger operations against another user's data by changing a URL, form value, cookie, API parameter, artist ID, album ID, or playlist ID.

User isolation applies equally to HTML routes, JSON/status endpoints, debug routes, background jobs, and error messages. Do not treat hiding UI controls as authorization.

### 3.3 Spotify connection

1. Each user connects their own Spotify account through Spotify OAuth.
2. The OAuth callback must associate the returned token with the initiating authenticated user and the one-time OAuth transaction.
3. A user must be able to disconnect Spotify. Disconnecting must remove/revoke stored credentials where supported and prevent future API calls for that user.
4. One user's token must never be used for another user's Spotify API request.
5. Token refresh and token replacement must be scoped to the owning user.
6. The UI must show connection state for the current user only.
7. Spotify API failures, revoked authorization, and expired credentials must be reported only to the affected user.
8. The configured redirect URI must remain stable and match Spotify's registered URI. It must not be built from an untrusted Host header.

If the app uses one shared Spotify developer application, its client ID/secret may be deployment-level configuration, while each user's refresh token remains private. If users are expected to supply their own Spotify developer credentials, those credentials must be stored per user and encrypted at rest. This is a product/deployment decision and must not be accidentally determined by the current settings form.

### 3.4 Scanning and scheduling

1. A scan is owned by exactly one user and operates only on that user's Spotify connection, configuration, state, and playlist.
2. A user's scan must not block, cancel, or overwrite another user's scan.
3. A user's scan must not be started twice concurrently unless the scan engine explicitly supports safe parallel runs for that same user.
4. Reorder and scan operations for the same user must retain the current mutual-exclusion guarantees because reorder is destructive to the playlist.
5. Different users may scan concurrently, subject to deployment resource limits and Spotify rate limits.
6. Scheduled scans must be created per user and use that user's schedule and enabled/connected status.
7. Scheduler jobs must be uniquely identified by user ID and safely reconciled when settings change, a user disconnects, or the app restarts.
8. A scan interrupted by restart must resume from that user's persisted progress without affecting others.
9. Cancel and status endpoints must address only the current user's job.
10. A user must not be able to trigger work for an arbitrary user ID.

The current in-process APScheduler design needs special attention if the deployment runs multiple web workers or replicas. Multiple processes must not each launch duplicate schedules. Choose one of:
- a dedicated scheduler/worker process with a persistent job store and distributed coordination; or
- a database-backed job queue/worker architecture.

A process-local scheduler is acceptable only if deployment is explicitly constrained to one scheduler instance and this constraint is enforced operationally. The design must also define concurrency limits and behavior when many users request scans at once.

### 3.5 Configuration and settings

1. Settings reads and writes must be scoped to the authenticated user.
2. A user changing settings must not change another user's settings.
3. Deployment-wide settings (database URL, secret keys, public URL, Spotify shared client credentials, scheduler limits) must be separated from user preferences.
4. User settings must have validation equivalent to the current app, including cron syntax, numeric ranges, and playlist ID validation.
5. Saving a user's schedule must update only that user's scheduled job.
6. Secrets must never be returned in full in HTML, JSON, logs, or error pages.
7. The UI must distinguish application-level configuration from user-level settings.

### 3.6 Album and artist actions

All existing user actions must become tenant-scoped:

- Exclude/include an album.
- Apply MusicBrainz pre-release exclusions and manual overrides.
- Toggle MusicBrainz active status for an artist.
- Run, cancel, and inspect scans.
- Create or reorder a playlist.
- View tracked artists, reports, upcoming releases, excluded albums, and debug artist results.

An ID supplied in a route is only a resource identifier, not proof of ownership. Resolve it inside the current user's state/database scope and return a safe 404/403 for resources not owned by that user. Do not leak whether another user has a particular album or artist.

### 3.7 User lifecycle

The implementation must define and enforce behavior for:

- New user registration or invitation.
- User disabled/suspended status.
- User deletion.
- Spotify disconnect.
- User data export and backup.
- Cleanup of that user's scheduled jobs, queued jobs, sessions, and cached data.

Deleting a user must not delete another user's data. The deletion policy for Spotify playlist contents must be explicit: deleting the app's local connection should not silently delete a user's Spotify playlist unless the user explicitly requests that action and confirms it.

## 4. Architecture requirements

### 4.1 Persistence: move from shared JSON files to a multi-tenant database

The current JSON files are single-instance files and are not an adequate concurrent multi-user database. A normalized relational database should become the source of truth. **Use PostgreSQL** for the multi-user implementation. It provides transactional integrity, relational constraints, concurrent access, and a deployment path that does not depend on a single application process or host. Do not use SQLite as the production multi-user database; it would constrain concurrency and complicate scaling. SQLite may still be useful for isolated unit tests, but integration tests should exercise PostgreSQL.

The schema must include an internal user table and user ownership on every user-specific record. At minimum, model:

- `users`: internal ID, identity-provider subject or normalized login identifier, status, created/updated timestamps.
- `user_sessions` or an equivalent server-side session store: session identifier/hash, user ID, creation/expiry/revocation timestamps.
- `user_spotify_connections`: user ID, Spotify account identity if available, encrypted refresh token, scopes, connected timestamp, token metadata.
- `user_settings`: user ID and validated user-level settings.
- `artists`: user ID plus Spotify artist ID and artist metadata; unique constraint on (user_id, spotify_artist_id).
- `albums`: user ID plus Spotify album ID and current app fields; unique constraint on (user_id, spotify_album_id).
- `musicbrainz_releases` or equivalent: user ID and release identifiers/override/handoff fields, with uniqueness scoped to user.
- `scan_runs`: user ID, status, progress, timestamps, cancellation state, error summary.
- `rate_limits`: user ID, endpoint category, retry-until timestamp.
- `logs`: user ID, timestamp, level/message, subject to retention limits.

Use a normalized schema rather than storing whole user states or settings as opaque JSON documents. Separate entities into related tables, use foreign keys, and avoid duplicating values that can be derived from relationships. JSON columns are acceptable only for genuinely variable provider payloads or versioned metadata that is not queried or independently updated.\n\nAll user-owned tables must carry a user ownership relationship directly or through a parent row, and the database must enforce tenant-safe relationships. Prefer composite unique keys and composite foreign keys where needed (for example, a child row referencing both its parent ID and user ID) so a row cannot accidentally point across tenants. Add indexes for user-scoped lookups and constraints that prevent cross-user duplicates or orphaned records. Normalize repeated artist, album, and connection data while retaining user-specific associations and overrides separately.

Shared reference data (for example static application version information) may remain global. MusicBrainz public catalog data may be cached globally only if the cache contains no user-specific overrides, ownership, or private Spotify data. Keep shared cache and user-specific decisions separate.

### 4.2 Storage and transaction boundaries

- Replace or supplement `JsonFileStore` with a database-backed implementation.
- Preserve a storage interface where it helps isolate business logic, but avoid forcing relational workflows into a file-shaped interface.
- Use transactions for multi-record updates, including album recording plus exclusion transfer and scan progress updates.
- Use atomic database updates or row locking for concurrent scan progress and overrides.
- Add versioned schema migrations and a documented upgrade/rollback strategy. PostgreSQL is the production target; tests must include PostgreSQL integration coverage.
- Do not store per-user state by placing user IDs into arbitrary filesystem paths as the primary isolation mechanism.
- If a transitional JSON import/export format is retained, it must be treated as a migration/backup format, not as a concurrent runtime database.

### 4.3 Request-scoped application context

Replace reliance on the process-wide default context for web requests. Each request must resolve the authenticated user and then construct or retrieve a context bound to that user's repository/storage operations and Spotify connection.

Preferred shape:
- application-level dependencies: database pool/session factory, OAuth configuration, encryption keys, scheduler/queue client;
- request-level identity: authenticated user record;
- user-scoped services/repositories: load settings, state, Spotify connection, and perform operations with explicit user ID.

Avoid mutating a global `AppContext` to switch users. That creates race conditions when requests overlap. Background jobs must receive an immutable user ID and reload the user's data when they execute.

### 4.4 Service and core API changes

Refactor service methods to require an explicit user context or user ID. Do not allow service methods to fall back silently to a default user's token or state.

Core operations that currently accept a shared context/state must receive a tenant-scoped repository/context. Review all functions that load/save state, config, tokens, logs, rate limits, scan progress, and playlist metadata.

The following areas need explicit review:
- `app.py`: every route and template context.
- `services.py`: scan and playlist orchestration.
- `spotify_core/context.py`: remove unsafe global tenant selection.
- `spotify_core/storage.py` and `spotify_core/state.py`: database-backed, owner-scoped persistence.
- `spotify_core/config.py`: split deployment configuration from user settings.
- `spotify_core/auth.py`: user-specific OAuth and token lifecycle.
- `spotify_core/scan.py`: user-specific locks, cancellation, progress, and job execution.
- `spotify_core/api.py`: user-specific rate-limit persistence and request pacing.
- `spotify_core/playlists.py`, `artists.py`, `musicbrainz.py`, and `reports.py`: tenant-safe reads and writes.
- Templates and static UI: login/logout, account connection status, and per-user settings.
- Docker Compose, Dockerfile, WSGI startup, health checks, and deployment docs.

## 5. Authentication and Cloudflare compatibility

Cloudflare Tunnel is required to expose the app through the CGNAT. It provides connectivity, not application identity. Authentication and authorization are handled by the application. Cloudflare Access is not required.

Required deployment behavior:
- Cloudflare may remain as a reverse proxy/WAF, but app authentication must be independently functional.
- If Cloudflare Access is retained as an optional outer gate, document that it is an additional layer and ensure the application still has its own authorization boundary.
- Do not trust user identity headers supplied by arbitrary clients. If a trusted proxy injects identity headers, restrict trust to that proxy and validate the mechanism; do not accept a plain email header from the public request.
- Set and document secure cookie attributes (`Secure`, `HttpOnly`, appropriate `SameSite`), trusted proxy configuration, HTTPS requirements, and allowed hostnames.
- Ensure OAuth callback routes are reachable through the chosen deployment path.
- Provide an operational way for the owner to bootstrap the first administrator without opening an unauthenticated admin endpoint.

The current Cloudflare Access/Google block is separate from the required tunnel connectivity. The multi-user implementation should not depend on Access being enabled or correctly configured.

## 6. Security and privacy requirements

1. Enforce authentication on every private route, including mutation and diagnostic endpoints. Public routes should be explicitly allowlisted (health check, sign-in, OAuth callback, static assets as needed).
2. Enforce authorization server-side for every user-owned resource.
3. Use CSRF protection on all state-changing browser requests, including login initiation where applicable, settings, overrides, scan controls, and disconnect.
4. Use secure, unpredictable session identifiers and rotate the session after login.
5. Store passwords only as strong salted hashes if password authentication is selected.
6. Encrypt Spotify refresh tokens and any user-supplied Spotify client secrets at rest. Keep encryption keys outside the database and backups; document rotation and recovery.
7. Never log access tokens, refresh tokens, client secrets, authorization codes, session cookies, or full sensitive callback URLs.
8. Validate OAuth state and, for OIDC, nonce and issuer/audience/signature/expiry.
9. Apply login throttling and abuse protection; avoid user enumeration in authentication and recovery responses.
10. Use least-privilege database credentials and protect database backups.
11. Add per-user and global limits for scan concurrency, request rates, log volume, and stored data.
12. Make error messages safe: do not disclose another user's resource existence, connection state, or configuration.
13. Define retention for logs, sessions, completed scan records, and deleted-user data.
14. Review dependencies and deployment headers for secure defaults.

## 7. Migration and backward compatibility

The existing deployment has one shared JSON dataset and one Spotify refresh token. Migration must preserve the current owner's data and avoid accidentally exposing it to new users.

Required migration behavior:

1. Take and verify a backup of the existing `/data` volume before upgrading.
2. Create the database schema using versioned migrations.
3. Provide an explicit first-run owner/bootstrap process.
4. Import the existing `app-config.json`, `spotify-state.json`, and `spotify-token.json` into the designated owner's records.
5. Preserve artist tracking, known albums, manual overrides, MusicBrainz data, playlist ID, rate-limit state, and resumable scan progress where safe.
6. Validate imported record counts and key invariants before marking migration complete.
7. Make migration idempotent: restarting after a partial migration must not duplicate records or silently overwrite newer data.
8. Keep the original files untouched until successful validation and a verified backup exist.
9. Do not automatically assign the existing dataset to whichever user happens to sign in first without an explicit, secure bootstrap decision.
10. Document downgrade limitations. Once multiple users have written data, exporting back to the legacy single-user JSON format is inherently lossy unless an explicit owner-only export is implemented.

Existing single-user behavior should be preserved for the migrated owner: same tracked artists, album history, manual overrides, playlist target, scan preferences, and Spotify connection, subject to Spotify requiring reauthorization.

## 8. User interface requirements

- Add sign-in and sign-out affordances.
- Provide an account/profile page showing the current identity and Spotify connection.
- Make it clear which Spotify account is connected, where Spotify exposes that identity.
- Keep dashboard, artist list, settings, debug page, logs, and album reports scoped to the signed-in user.
- Provide per-user scan status and progress.
- Display useful empty states for a new user with no Spotify connection or no scan history.
- Ensure errors and disconnected states guide the user to reconnect without exposing secrets.
- Do not expose user IDs in links unless needed; never rely on client-provided IDs for authorization.
- Add an administrative interface only if user-management requirements are approved. Admin views must not expose Spotify tokens.

## 9. Operational and deployment requirements

- Use PostgreSQL as the production database and document the supported deployment topology.
- Configure persistent storage and backup/restore for the database.
- Provide environment variables or secrets management for database credentials, session signing key, token encryption key, public base URL, OAuth client credentials (if shared), and bootstrap settings.
- Ensure secrets are not baked into the image or committed to the repository.
- If running multiple web workers, use shared session storage and shared database state.
- Run scheduled work in one coordinated worker/scheduler system; do not depend on process-local locks across workers.
- Add health checks for web and worker components, and expose only non-sensitive health details.
- Define graceful shutdown behavior for active scans and job leases.
- Add metrics/logging for queue depth, per-user scan failures, scheduler health, and database errors without leaking private data.
- Document backup restore, key rotation, database migration, user removal, and recovery from a failed deployment.
- Set resource limits and fair-use controls so one user's scan cannot starve all other users.

## 10. Testing requirements and acceptance criteria

### Authentication
- Anonymous requests to every private route are redirected or rejected.
- Valid login creates a session; invalid credentials/identity assertions are rejected.
- Session rotation occurs after authentication.
- Logout invalidates the session.
- Expired, revoked, malformed, replayed, or wrong-audience identity/OAuth state is rejected.
- CSRF attempts against every mutation route fail.

### Tenant isolation
Create at least two test users with distinct Spotify connections, settings, artists, albums, overrides, logs, and scan progress.

- User A cannot read or mutate User B's settings, artists, albums, logs, or scan status.
- Forging User B's ID in paths, forms, query strings, or API payloads does not grant access.
- User A's dashboard and reports contain no User B data.
- User A's Spotify token is never used for User B's request, and vice versa.
- A user cannot change the playlist ID or overrides of another user.
- Database constraints reject cross-tenant references and duplicate keys within the same tenant.

### Scanning and concurrency
- Two users can scan independently.
- A scan for one user does not prevent another user's scan unless the configured global concurrency limit is reached.
- Duplicate scans for the same user are prevented or safely serialized.
- Canceling one user's scan does not cancel another's.
- Reorder and scan cannot conflict for the same user.
- A worker restart recovers or safely marks in-flight jobs; progress remains associated with the correct user.
- Scheduler restart does not create duplicate jobs.
- Updating one user's cron schedule does not alter another user's schedule.

### Data and migration
- Migration from representative legacy JSON fixtures preserves all supported fields.
- Migration can be safely retried after interruption.
- A failed migration leaves source files intact and reports actionable errors.
- User deletion removes or anonymizes only the selected user's data and jobs.
- Backup/restore preserves tenant ownership and encrypted credentials remain unusable without the external key.

### Regression
- Existing scan, MusicBrainz, playlist, ordering, manual override, and report tests continue to pass after adapting fixtures to user-scoped storage.
- Add route tests for authentication and tenant ownership.
- Add integration tests using two users and separate mocked Spotify accounts.
- Add tests for the actual production worker/scheduler topology, not only a single-process test setup.

## 11. Recommended implementation sequence

This sequence is intended to keep the work reviewable and prevent a partial multi-user conversion from accidentally exposing shared data.

### Phase 0 — Confirm product and deployment decisions
- Decide registration/invitation policy and whether there is an administrator role.
- Decide whether Spotify developer credentials are shared deployment credentials or supplied by each user.
- Decide the application's authentication and account-admission policy (see Section 12). Cloudflare Access is not part of that decision.
- Decide SQLite single-instance versus PostgreSQL/multi-worker support.
- Decide whether multiple users may scan concurrently and define global limits.
- Decide user deletion and data export semantics.

### Phase 1 — Introduce identity and authorization foundation
- Add user model/repository and authentication flow.
- Add server-side session lifecycle and CSRF protection.
- Add route-level authentication and explicit public-route allowlist.
- Add authorization helpers and tests before exposing any multi-user data.
- Do not yet allow multiple users to share the existing JSON state.

### Phase 2 — Database and migration
- Add schema migrations and tenant ownership constraints.
- Implement repositories and transactional persistence.
- Implement legacy single-user import into a designated owner.
- Verify migration with fixtures and a copy of a real-shaped dataset.

### Phase 3 — Make core operations tenant-aware
- Refactor context and service APIs to require user scope.
- Scope config, tokens, state, logs, rate limits, and all Spotify operations.
- Scope scan/reorder locks, cancellation, progress, and scheduled jobs.
- Audit every core module for implicit default-context access.

### Phase 4 — Convert routes and UI
- Require identity on private routes.
- Pass only current-user data into templates.
- Add account and Spotify connection management.
- Verify all mutating routes enforce ownership and CSRF.

### Phase 5 — Worker/scheduler and production hardening
- Deploy coordinated worker/scheduler and shared persistence.
- Add concurrency/fair-use controls, graceful shutdown, health checks, and operational documentation.
- Validate Cloudflare Tunnel connectivity through the CGNAT and verify application-managed authentication and tenant authorization.

### Phase 6 — End-to-end verification and rollout
- Run unit, route, integration, migration, and deployment tests.
- Test two simultaneous users with distinct Spotify accounts.
- Back up production data and perform a rehearsed migration.
- Roll out with a documented rollback path and monitor scheduler, database, OAuth, and scan errors.

## 12. Decisions that must be made before implementation

These are intentionally open; the code should not silently choose for the product owner.

1. **Who can create an account?** Public self-registration, invite-only, or administrator-created users?
2. **Who is an administrator?** Is there a single owner/admin, and what can that role see or change?
3. **Authentication provider:** Local email/password, email magic link, non-Google OIDC, or another provider? What recovery path is required?
4. **Spotify OAuth application:** One shared Spotify developer app for all users, or each user supplies their own client credentials?
5. **Database/deployment:** PostgreSQL is the chosen production database. Confirm whether the supported deployment must include multiple app replicas/workers; the design should use shared database-backed sessions and coordinated background workers regardless.
6. **Scan scheduling:** Should each user have an independent schedule? Should scans be disabled until Spotify is connected?
7. **Concurrency policy:** Maximum simultaneous scans globally and per user; queue or reject excess requests?
8. **User deletion:** Retain an audit tombstone, fully delete user data, or support export before deletion?
9. **Spotify playlist ownership:** On disconnect/deletion, leave playlists untouched by default? (Recommended as the safe default.)
10. **Cloudflare role:** Is Cloudflare Access to be removed entirely, retained as an optional outer gate, or configured with a different IdP? The app must not depend on it for its own tenant authorization.

## 13. Definition of done

The conversion is complete only when:

- Multiple users can reach the app through Cloudflare Tunnel, and the application independently enforces authentication and tenant authorization.
- Each user can connect and disconnect their own Spotify account.
- All settings, state, reports, logs, jobs, overrides, and playlist operations are isolated by user.
- Two users can use the app concurrently without cross-account reads, writes, token use, or job cancellation.
- The legacy single-user data has a tested, repeatable migration path.
- Authentication, authorization, CSRF, token protection, and tenant isolation have automated tests.
- The supported production topology, backup/restore, secrets, and operational procedures are documented.
- Existing single-user functionality remains covered by regression tests.

## 14. Repository-specific audit checklist

Before implementation is considered ready, search for and eliminate or justify every use of:

- `core.load_config()`, `core.save_config()`, `core.load_state()`, `core.save_state()`, and `core.load_refresh_token()` that does not receive an authenticated user scope.
- Module-level `run_lock`, `reorder_lock`, and cancellation events used as if they represented all users.
- Process-global rate limiter state when it should be user- or deployment-scoped.
- Module-level recent log buffers that mix users.
- Direct reads/writes of `/data/*.json` outside migration/backup compatibility code.
- Routes that mutate state without authentication, CSRF validation, and ownership checks.
- Any job closure or queued task that captures mutable request data instead of a stable user ID.
- Any template or API response that serializes secrets or unfiltered global state.

This checklist should be updated during implementation as the codebase changes. A passing test suite is necessary but not sufficient: the two-user isolation tests and deployment-topology tests are release gates.

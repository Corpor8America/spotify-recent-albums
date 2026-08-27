# CI Pipeline

This document describes exactly how this repository's CI/CD works. It is written
so it can be copied into another project and used as a blueprint: an agent (or
human) should be able to reproduce the same setup by following it.

## Overview

There are two workflows that chain together:

```
push / PR to main
        |
        v
+------------------+     success on main only      +---------------------------+
|       CI         | ----------------------------> |   Build, Tag, Publish     |
|  .github/        |      (workflow_run trigger)    |   .github/workflows/      |
|  workflows/ci.yml|                               |  docker-publish.yml       |
+------------------+                               +---------------------------+
| 1. Unit tests                                    | Runs ONLY when:           |
| 2. Docker integration tests                      |  - CI succeeded           |
|    (compose up, hit real HTTP endpoints)         |  - branch == main         |
+------------------+                               |  - VERSION is not already |
                                                   |    tagged / published     |
                                                   | Then: build image, push   |
                                                   | to ghcr.io, git tag vX,   |
                                                   | create GitHub Release     |
                                                   +---------------------------+
```

Key design decisions:

1. **Version lives in a plain-text `VERSION` file at the repo root** (e.g.
   `1.6.0`). No tags-in-git-first, no release branches, no CI-bumping of
   files. To release, you bump `VERSION` and merge to main.
2. **Publishing is version-gated.** The publish workflow compares the `VERSION`
   file against existing git tags *and* against images already in GHCR. If the
   version was already released, it exits cleanly without doing anything. This
   makes the pipeline idempotent: every push to main runs it, but it only
   actually publishes once per unique version.
3. **Tests gate publishes structurally**: the publish workflow is triggered by
   `workflow_run` on CI completing successfully, so a broken main branch can
   never publish.

---

## File inventory

```
VERSION                          # single line: "1.6.0" (no "v" prefix)
.github/
  workflows/
    ci.yml                       # unit tests + docker integration tests
    docker-publish.yml           # version-gated GHCR publish + release
docker-compose.dev.yml           # integration-test environment (app + mock deps)
tests/test_integration_docker.py # integration tests, gated behind env var
```

---

## Workflow 1: CI (`ci.yml`)

### Triggers and permissions

```yaml
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

permissions:
  contents: read
```

Runs on every PR targeting main and every push to main. Read-only token.

### Job 1: `test` (unit tests)

Plain interpreter-level tests, no Docker:

1. `actions/checkout@v4`
2. `actions/setup-python@v5` (pin the language version this project uses)
3. Install dependencies (`pip install -r requirements.txt`)
4. Run the test suite (`python -m unittest discover tests`)

The important convention here: **integration tests must be excludable from
this run** so they don't fail because no containers are up. In this project
that's done with an environment-variable gate on the test class:

```python
@unittest.skipUnless(os.environ.get("INTEGRATION_TEST"),
                     "Set INTEGRATION_TEST=1 to run Docker integration tests")
class DockerIntegrationTests(unittest.TestCase): ...
```

(With pytest the equivalent would be a marker + `-m "not integration"`.)
Whatever the mechanism, unit CI just runs the default suite and the
integration module self-skips.

### Job 2: `integration-test` (Docker integration tests)

```yaml
needs: test          # only run if unit tests passed
runs-on: ubuntu-latest
```

Steps, in order:

1. **Checkout**
2. **Set up Docker Buildx** — `docker/setup-buildx-action@v3`
3. **Build and start the full stack**:
   ```
   docker compose -f docker-compose.dev.yml up -d --build
   ```
   This builds the app image from the local `Dockerfile` (i.e. the exact
   artifact that will later be published) and starts any mock/dependency
   services alongside it. In this repo that's a mock Spotify API server; in
   another project it would be whatever fakes/stubs the external APIs or
   databases the app talks to. Design rules for this compose file:
   - App container gets its external dependencies pointed at the mocks via
     env-var overrides (`SPOTIFY_API_BASE_OVERRIDE=...` etc.).
   - Mocks get healthchecks; the app uses
     `depends_on: <mock>: condition: service_healthy`.
   - Scheduler/background jobs are disabled via env var (`RUN_SCHEDULER: "0"`)
     so tests control timing explicitly.
   - Persisted state is bind-mounted from `./tests/seed` so tests can read and
     reset the app's state directly from the host filesystem.
4. **Wait for app readiness** — poll a cheap status endpoint until it answers,
   with a hard timeout so the job fails fast instead of hanging:
   ```bash
   timeout 60s bash -c 'until curl -s -f http://localhost:8080/status; do echo "Waiting..."; sleep 5; done'
   ```
5. **Set up Python + install only what the tests need** (e.g. `requests`). The
   test runner runs on the host, against the containers over HTTP.
6. **Run the integration tests**, setting the gate env var:
   ```bash
   INTEGRATION_TEST=1 python -m unittest tests/test_integration_docker.py -v
   ```
7. **On failure, dump logs** — invaluable for debugging compose-based tests:
   ```yaml
   - name: Collect Logs on Failure
     if: failure()
     run: docker compose -f docker-compose.dev.yml logs
   ```
8. **Always tear down**:
   ```yaml
   - name: Stop Containers
     if: always()
     run: docker compose -f docker-compose.dev.yml down
   ```

Integration-test authoring conventions worth copying:

- Tests talk to the app purely over HTTP (`INTEGRATION_APP_URL`, default
  `http://localhost:8080`) and to mocks over their control endpoints
  (`/_control/snapshot`, `/configure`, ...).
- Stateful tests reset persisted state before running (restore seed files),
  and first wait for background work to settle so async tasks from a previous
  test can't clobber the reset.
- Polling helpers have deadlines everywhere; nothing waits forever.

---

## Workflow 2: Publish (`docker-publish.yml`)

### Trigger: chained after CI

```yaml
on:
  workflow_run:
    workflows: ["CI"]       # must match ci.yml's `name:` exactly
    types: [completed]

permissions:
  contents: write            # to push git tags
  packages: write            # to push to GHCR
```

`workflow_run` fires when CI finishes, regardless of result, so the first job
step re-checks everything:

```yaml
if: github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.head_branch == 'main'
```

Two guards: CI must have **passed**, and the commit must be on **main**
(PRs never publish).

Checkout uses the exact SHA CI ran against, with full history so tag lookups work:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
    ref: ${{ github.event.workflow_run.head_sha }}
```

### Version gating (the heart of it)

Read the version:

```bash
echo "app_version=$(cat VERSION | tr -d '[:space:]')" >> $GITHUB_OUTPUT
```

Then two independent idempotency checks; either one being true means skip:

**Check 1 — does git tag `v<version>` already exist?**

```bash
if git rev-parse "v${VERSION}" >/dev/null 2>&1; then exists=true; fi
```

If yes → step exits 0 early ("Version already tagged. Skipping build.").

**Check 2 — does an image with this version tag already exist in GHCR?**

Uses `gh api` (preinstalled on runners) against the package versions endpoint:

```bash
found=$(gh api "/users/${OWNER}/packages/container/${PKG_NAME}/versions" \
  --jq '[.[].metadata.container.tags[]? | select(. == "'"$ver"'" or . == "v'"$ver"'")] | length' \
  2>/dev/null || echo 0)
```

This covers the case where the image got published but tagging/release failed
afterwards — a re-run won't republish the same version.

Only if both checks say "new version" does anything happen.

### Build, push, tag, release

1. **Login to GHCR** using the built-in token (no secrets to configure):
   ```yaml
   - uses: docker/login-action@v3
     with:
       registry: ghcr.io
       username: ${{ github.actor }}
       password: ${{ secrets.GITHUB_TOKEN }}
   ```
2. **Metadata/tagging** (`docker/metadata-action@v5`) — pushes four tags every
   release:
   | Tag | Meaning |
   |---|---|
   | `latest` | always newest release |
   | `<version>` e.g. `1.6.0` | exact version |
   | `v<version>` e.g. `v1.6.0` | exact version, v-prefixed |
   | `major.minor` e.g. `1.6` | floating minor tag |

   Plus OCI label `org.opencontainers.image.version`.
3. **Build & push** (`docker/build-push-action@v6`) with GitHub Actions cache:
   ```yaml
   cache-from: type=gha
   cache-to: type=gha,mode=max
   ```
   Image name: `ghcr.io/${{ github.repository }}`.
4. **Create and push the git tag** `v<version>` as the actions bot — only if
   check 1 said it didn't exist:
   ```bash
   git config user.name "github-actions[bot]"
   git config user.email "github-actions[bot]@users.noreply.github.com"
   git tag "v${VERSION}" && git push origin "v${VERSION}"
   ```
5. **Create a GitHub Release** (`softprops/action-gh-release@v2`) with auto
   generated notes. Versions containing `-` (e.g. `1.7.0-rc1`) are marked
   pre-release.

---

## How to release

1. Change code, open PR → CI runs unit + integration tests on the PR.
2. Bump `VERSION` (single line, no `v` prefix) as part of the change, merge to main.
3. CI runs again on main; on success the publish workflow fires automatically:
   - new version → image pushed to `ghcr.io/<owner>/<repo>:1.7.0` (+ `latest`,
     `v1.7.0`, `1.7`), git tag `v1.7.0` created, GitHub Release created.
   - unchanged version → workflow completes in seconds having done nothing.

Forgetting to bump `VERSION` means nothing is published — that is the feature.

---

## Porting checklist (for a new project)

To replicate this in another repo, produce/adjust these pieces:

- [ ] **`VERSION`** file at repo root, contents = semver string, no prefix.
- [ ] **Unit tests** that run headless and skip integration tests by default
      (env-var gate or pytest marker).
- [ ] **`tests/test_integration_docker.py`** (or equivalent) gated behind
      `INTEGRATION_TEST=1`, driving the app over HTTP only, with deadline-
      bounded polling helpers and seed-file state resets if the app persists
      data.
- [ ] **`docker-compose.dev.yml`**: app service built from the local
      `Dockerfile` (same artifact that ships), mock/dependency services with
      healthchecks, app pointed at mocks via env overrides, background jobs
      disabled, persistent state bind-mounted from `tests/seed`.
- [ ] **`.github/workflows/ci.yml`**: as above; adjust language/runtime,
      install command, test command, readiness URL/port, and the integration
      test invocation.
- [ ] **`.github/workflows/docker-publish.yml`**: copy nearly verbatim;
      the only things that ever need changing are the `workflows: ["CI"]`
      name reference and, if the registry/package layout differs, the `gh api`
      package path.
- [ ] **First release gotcha:** the very first publish has no prior tag, which
      is fine — but make sure the package becomes public/visible as desired
      (GHCR packages linked to the repo inherit its visibility initially;
      adjust in package settings afterwards).
- [ ] Repo settings: allow Actions read/write permission for `contents` and
      `packages` (Settings → Actions → General → Workflow permissions) so the
      bot can push tags and images.

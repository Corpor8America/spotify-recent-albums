# Spotify Recently Released Albums

A self-hosted web application that keeps a Spotify playlist up to date with recent albums from artists you follow. It discovers releases, tracks which albums are known, and synchronizes eligible albums to a playlist on your Spotify account.

## Features

- **Automatic artist scans:** checks followed artists on a configurable schedule and supports starting or cancelling a scan from the dashboard.
- **Recent-release tracking:** finds albums within the configured lookback period and keeps the playlist ordered with the newest releases first.
- **Playlist management:** creates a Spotify playlist or synchronizes an existing playlist.
- **Include and exclude controls:** manually include or exclude albums from the dashboard.
- **Upcoming releases:** uses MusicBrainz release information to identify prerelease albums and lets you manually exclude an upcoming release.
- **Expired albums:** moves albums beyond the configured lookback age out of the main playlist into an Expired section. You can promote an expired album back to the playlist or return it to Expired. Expired entries are retained for twice the configured lookback period.
- **Artist status:** view followed artists, scan progress, last-checked information, and MusicBrainz activity.
- **Operational visibility:** dashboard scan status, a dedicated Activity page for logs, plus health and readiness endpoints.
- **Persistent configuration and state:** settings, authorization, and scan data are stored in the mounted data directory.

## Requirements

- Docker Engine and the Docker Compose plugin.
- A Spotify account.
- A Spotify application created in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) with a client ID and client secret.
- A reachable URL for the app that matches the Spotify redirect URI. For production Compose, the included Caddy service provides HTTPS on port 8443.

## Install and run

### Production deployment

The production Compose file pulls the published container image and runs it behind Caddy.

1. Clone this repository and enter the project directory.
2. Start the services:

   ```bash
   docker compose up -d
   ```

3. Open `https://<your-host-or-LAN-IP>:8443`.

Caddy uses its internal certificate authority. To avoid browser certificate warnings, export its root certificate and trust it on each device used to access the app:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

The production app listens internally on port 8080; Caddy publishes port 8443. The app and Caddy each use a named Docker volume.

### Local deployment

For a local build without the Caddy proxy:

```bash
docker compose -f docker-compose.local.yml up -d --build
```

Open `http://127.0.0.1:8081`. This deployment uses its own `spotify_local_data` volume, separate from the production data volume.

### Development deployment

The development Compose file builds the app and starts a mock Spotify API server. It uses seeded test data and does not call the real Spotify API.

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

Open `http://127.0.0.1:8081`. The app's host port can be changed with `APP_PORT`. The development configuration disables the scheduler and sets API request delay to zero.

## Configure Spotify

1. In the Spotify Developer Dashboard, create an application and copy its **Client ID** and **Client Secret**.
2. Add the app's callback URL as a **Redirect URI** in the Spotify application settings. It must exactly match the app's public base URL followed by `/callback`.
   - Production: `https://<your-host-or-LAN-IP>:8443/callback`
   - Local: `http://127.0.0.1:8081/callback`
3. Open the app's **Settings** page at `/settings`.
4. Enter the Client ID and Client Secret. Set the public base URL to the URL you use to access the app if the displayed default is not correct.
5. Optionally enter a Spotify Playlist ID. You can also use **Create playlist** to create a playlist and save it as the synchronization target.
6. Save the settings, then choose **Connect Spotify account** and authorize the application.

Spotify requires an exact redirect URI match. For local HTTP access, use `127.0.0.1`; for a LAN address in production, use the HTTPS URL served by Caddy.

## Configure scanning

The Settings page controls the scan and synchronization behavior:

- **Check each artist every N days:** how often an artist becomes due for another check.
- **Days lookback:** the age window used to select recent albums and determine when albums become expired.
- **Cron schedule:** a five-field cron expression in UTC that controls the automatic scan schedule. The default is `0 6 * * *` (6:00 UTC daily).
- **Minimum seconds between API requests:** controls the delay between API requests.
- **Verbose logging:** enables additional diagnostic logging.

Scheduled scans include a randomized delay of up to 15 minutes by default. You can also use **Run scan now** on the dashboard. A scan already in progress is not started a second time.

## Use the dashboard

After configuration and Spotify authorization:

- The dashboard shows recent albums, excluded albums, expired albums, and scan status. The Activity page shows recent logs and can optionally auto-refresh.
- Use an album's include/exclude controls to manage whether it belongs in the synchronized playlist.
- Use the Expired section to promote an album back to the main playlist or return a promoted album to Expired.
- Visit **Artists** to review followed artists and their scan status.
- Use **Run scan now** to start a scan immediately, or **Cancel** to request cancellation.
- Use the reorder action to rebuild the playlist order from the app's current album state.

The app needs a configured Spotify playlist to write playlist changes. Leaving the playlist ID blank allows the app to run without playlist synchronization.

## Data and backups

The application stores its data under `/data` in the container. The Docker Compose deployments mount this directory to a named volume, so data survives container replacement and upgrades as long as the volume is retained.

Back up the production data volume with:

```bash
docker run --rm \
  -v spotify-recently-released-albums_spotify_data:/data \
  -v "$(pwd):/backup" \
  alpine tar czf /backup/spotify-data-backup.tar.gz -C /data .
```

Use the corresponding volume name if you are backing up a local deployment. Keep backups somewhere outside the Docker volume.

## Develop and test

The application is written in Python and uses Flask, Requests, APScheduler, and Gunicorn. Python 3.12 or newer is required for local development.

Create an environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the test suite:

```bash
python -m pytest
```

Run lint checks with Ruff:

```bash
ruff check .
```

For end-to-end development against the mock Spotify service, use the development Compose deployment described above. The mock server exposes a control snapshot at `/_control/snapshot` on port 8791.

The production container runs the WSGI application with one Gunicorn worker. Keep the single-worker configuration unless the scheduler and scan lock are moved out of the web process; they are process-local.

## Health endpoints

- `/healthz` — liveness check.
- `/readyz` — readiness check; returns ready once a Spotify client ID is configured.
- `/status` — JSON status including connection state, scan state, rate limits, known album count, and recent logs.

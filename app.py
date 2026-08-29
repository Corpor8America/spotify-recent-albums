"""Flask web app for the Spotify Recently Released Albums service.

Importing this module has no side effects: the Flask app and the
APScheduler background thread are only created by ``create_app()``
(called from ``wsgi.py`` under gunicorn, or the ``__main__`` guard below).
"""

import os
import re
import secrets
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import spotify_core as core
from services import PlaylistService, ScanService


def cfg():
    return core.load_config()


def public_base_url():
    explicit = (cfg().get("public_base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return request.host_url.rstrip("/")


def redirect_uri():
    return f"{public_base_url()}/callback"


def _get_creds():
    c = cfg()
    return c["spotify_client_id"], c["spotify_client_secret"]


def _validate_cron_schedule(cron_schedule):
    """Return the 5 cron fields if the schedule is valid, else raise ValueError."""
    from apscheduler.triggers.cron import CronTrigger

    parts = cron_schedule.split()
    if len(parts) != 5:
        raise ValueError("cron_schedule must have 5 fields (minute hour day month day-of-week)")
    try:
        CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])
    except Exception as e:
        raise ValueError(f"invalid cron_schedule {cron_schedule!r}: {e}")
    return parts


def _parse_int_field(form, key, default, min_value):
    raw = form.get(key)
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer")
    if value < min_value:
        raise ValueError(f"{key} must be >= {min_value}")
    return value


def _parse_float_field(form, key, default, min_value):
    raw = form.get(key)
    try:
        value = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if value < min_value:
        raise ValueError(f"{key} must be >= {min_value}")
    return value


def format_rate_limit_until(ts):
    now = datetime.now().astimezone()
    until = datetime.fromtimestamp(ts).astimezone()
    remaining = max(0, int(ts - now.timestamp()))
    if remaining >= 3600:
        relative = f"about {(remaining + 3599) // 3600}h"
    elif remaining >= 60:
        relative = f"about {(remaining + 59) // 60}m"
    else:
        relative = f"{remaining}s"
    return f"{until.strftime('%Y-%m-%d %I:%M:%S %p')} ({relative})"


def _format_last_checked(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return iso_str or "-"
    now = datetime.now(timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = seconds // 86400
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return dt.strftime("%Y-%m-%d")


def create_app():
    """Application factory: builds the Flask app and starts the scheduler."""
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.secret_key = cfg()["flask_secret_key"]

    scans = ScanService()
    playlists = PlaylistService()

    # --- Settings / first-run setup ------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            existing = cfg()
            try:
                c = {
                    "spotify_client_id": (request.form.get("spotify_client_id") or existing["spotify_client_id"]).strip(),
                    "spotify_client_secret": (request.form.get("spotify_client_secret") or existing["spotify_client_secret"]).strip(),
                    "spotify_playlist_id": request.form.get("spotify_playlist_id", "").strip(),
                    "interval_days": _parse_int_field(request.form, "interval_days", existing["interval_days"], min_value=1),
                    "min_request_interval": _parse_float_field(request.form, "min_request_interval", existing["min_request_interval"], min_value=0),
                    "days_lookback": _parse_int_field(request.form, "days_lookback", existing["days_lookback"], min_value=0),
                    "cron_schedule": request.form.get("cron_schedule", existing["cron_schedule"]).strip(),
                    "public_base_url": request.form.get("public_base_url", existing["public_base_url"]).rstrip("/"),
                    "verbose_logging": request.form.get("verbose_logging") == "true",
                }
                _validate_cron_schedule(c["cron_schedule"])
                if c["spotify_playlist_id"] and not re.fullmatch(r"[A-Za-z0-9]{15,}", c["spotify_playlist_id"]):
                    raise ValueError("spotify_playlist_id must be alphanumeric (or empty)")
            except ValueError as e:
                return f"Invalid settings: {e}", 400
            core.save_config(c)
            core.log("Settings saved.")
            return redirect(url_for("dashboard"))
        return render_template("settings.html", config=cfg(), version=core.get_version(),
                               effective_public_base_url=public_base_url(),
                               connected=core.is_connected())

    @app.route("/create_playlist", methods=["POST"])
    def create_playlist():
        name = (request.form.get("playlist_name") or "Recently Released Albums").strip()
        ok, error = playlists.create(name)
        if not ok:
            status = 400 if error == "Not connected to Spotify" else 500
            return error, status
        return redirect(url_for("settings"))

    # --- Dashboard ---------------------------------------------------------------

    @app.route("/")
    def dashboard():
        if not core.is_configured():
            return redirect(url_for("settings"))

        state = core.load_state()
        if core.clear_expired_rate_limits(state):
            core.save_state(state)
        c = cfg()
        return render_template(
            "dashboard.html",
            connected=core.is_connected(),
            playlist_id=c["spotify_playlist_id"],
            report_albums=core.get_report_albums(state, c["days_lookback"]),
            excluded_albums=core.get_excluded_albums(state),
            in_progress=state.in_progress,
            rate_limits={
                cat: format_rate_limit_until(ts)
                for cat, ts in state.rate_limits.items()
            },
            artists_tracked=len(state.artists),
            known_albums_count=len(state.known_albums),
            logs=core.get_recent_logs()[-80:],
            scan_running=core.run_lock.locked(),
            reorder_running=core.reorder_lock.locked(),
            now=datetime.now(timezone.utc),
            version=core.get_version(),
        )

    # --- OAuth -------------------------------------------------------------------

    @app.route("/login")
    def login():
        client_id, _ = _get_creds()
        if not client_id:
            return "Spotify Client ID not configured. Go to /settings first.", 500
        csrf_state = secrets.token_urlsafe(16)
        session["oauth_state"] = csrf_state
        return redirect(core.get_auth_url(client_id, redirect_uri(), csrf_state))

    @app.route("/callback")
    def callback():
        error = request.args.get("error")
        if error:
            return f"Spotify authorization failed: {error}", 400

        if request.args.get("state") != session.get("oauth_state"):
            return "State mismatch -- possible CSRF, please try /login again.", 400

        code = request.args.get("code")
        if not code:
            return "Missing authorization code.", 400

        client_id, client_secret = _get_creds()
        token_data = core.exchange_code_for_token(client_id, client_secret, code, redirect_uri())
        core.save_refresh_token(token_data["refresh_token"])
        core.log("Connected to Spotify via OAuth.")
        return redirect(url_for("dashboard"))

    # --- Scan actions ------------------------------------------------------------

    @app.route("/run", methods=["POST"])
    def run_now():
        scans.trigger_now(cfg())
        return redirect(url_for("dashboard"))

    @app.route("/cancel", methods=["POST"])
    def cancel_scan():
        scans.cancel()
        return redirect(url_for("dashboard"))

    @app.route("/reorder", methods=["POST"])
    def reorder_playlist():
        playlists.reorder_async()
        return redirect(url_for("dashboard"))

    # --- Album overrides ---------------------------------------------------------

    @app.route("/albums/<album_id>/override", methods=["POST"])
    def set_override(album_id):
        value = request.form.get("value")
        known = playlists.apply_override(album_id, value)
        if not known:
            return "Unknown album", 404
        return redirect(url_for("dashboard"))

    # --- Followed Artists --------------------------------------------------------

    @app.route("/artists")
    def artists_list():
        if not core.is_configured():
            return redirect(url_for("settings"))
        state = core.load_state()
        due_ids = set(state.in_progress.due_ids) if state.in_progress else set()
        artists = sorted(
            [
                {
                    "id": aid,
                    "name": info.name or aid,
                    "last_checked": info.last_checked,
                    "last_checked_display": _format_last_checked(info.last_checked),
                    "scanned_with": info.scanned_with,
                    "is_due": aid in due_ids,
                    "is_processed": state.in_progress is not None and aid not in due_ids,
                    "musicbrainz_id": info.musicbrainz_id,
                    "mb_active": info.mb_active,
                }
                for aid, info in state.artists.items()
            ],
            key=lambda a: a["name"].lower(),
        )
        return render_template("artists.html", artists=artists, version=core.get_version(),
                               scan_running=core.run_lock.locked(),
                               scan_in_progress=state.in_progress is not None)

    @app.route("/artists/<artist_id>/toggle-active", methods=["POST"])
    def toggle_active(artist_id):
        if not core.is_configured():
            return redirect(url_for("settings"))
        state = core.load_state()
        artist = state.artists.get(artist_id)
        if not artist:
            return "Unknown artist", 404
        artist.mb_active = not artist.mb_active
        artist.mb_active_checked = ""  # Force re-check on next scan
        core.save_state(state)
        core.log(f"Toggled active status for {artist.name}: {'active' if artist.mb_active else 'inactive'}")
        return redirect(url_for("artists_list"))

    # --- Debug / Artist Inspector ------------------------------------------------

    @app.route("/debug/artist", methods=["GET", "POST"])
    def debug_artist():
        if not core.is_configured():
            return redirect(url_for("settings"))

        result = None
        error = None
        artist_input = ""

        if request.method == "POST":
            artist_input = (request.form.get("artist_input") or "").strip()

            match = re.search(r"open\.spotify\.com/artist/([A-Za-z0-9]+)", artist_input)
            if match:
                artist_id = match.group(1)
            elif re.fullmatch(r"[A-Za-z0-9]{22}", artist_input):
                artist_id = artist_input
            else:
                error = "Enter a Spotify artist ID (22-char alphanumeric) or a full Spotify artist URL."
                return render_template("debug_artist.html", artist_input=artist_input, result=result, error=error, version=core.get_version())

            try:
                client_id, client_secret = _get_creds()
                refresh_token = core.load_refresh_token()
                token = core.get_access_token(client_id, client_secret, refresh_token)
                state = core.load_state()

                artist_name = artist_id
                try:
                    artist_data = core.spotify_get(token, f"{core.get_context().spotify_api_base}/artists/{artist_id}", state)
                    artist_name = artist_data.get("name", artist_id)
                except Exception:
                    pass

                albums = core.get_artist_albums(token, artist_id, state)
                parsed = []
                for a in albums:
                    rd = core.parse_release_date(a.get("release_date", ""))
                    now_date = datetime.now().date()
                    is_future = rd is not None and rd.date() > now_date
                    known = state.known_albums.get(a["id"])
                    parsed.append({
                        "id": a["id"],
                        "name": a.get("name"),
                        "album_type": a.get("album_type"),
                        "release_date": a.get("release_date"),
                        "parsed_date": rd.strftime("%Y-%m-%d") if rd else "N/A",
                        "total_tracks": a.get("total_tracks"),
                        "is_future": is_future,
                        "in_state": known is not None,
                        "url": a.get("external_urls", {}).get("spotify", ""),
                        "artists": ", ".join(ar.get("name", "?") for ar in a.get("artists", [])),
                        "raw": {k: a[k] for k in ("id", "name", "album_type", "release_date", "total_tracks") if k in a},
                    })

                mb_info = None
                try:
                    mbid = core.resolve_spotify_to_mb(artist_id)
                    if mbid:
                        ctx = core.get_context()
                        mb_active = core.get_artist_active(mbid)
                        mb_release_groups = core.get_artist_release_groups(ctx, mbid)
                        mb_upcoming = core.get_albums_with_future_dates(ctx, mbid)
                        mb_albums = []
                        for rg in mb_release_groups:
                            mb_albums.append({
                                "id": rg.get("id"),
                                "name": rg.get("title"),
                                "primary_type": rg.get("primary-type"),
                                "release_date": rg.get("first-release-date", ""),
                                "is_upcoming": rg.get("id", "") in {u.get("id") for u in mb_upcoming},
                                "url": f"https://musicbrainz.org/release-group/{rg.get('id')}",
                            })
                        mb_info = {
                            "mbid": mbid,
                            "active": mb_active,
                            "album_count": len(mb_albums),
                            "upcoming_count": len(mb_upcoming),
                            "albums": mb_albums,
                            "url": f"https://musicbrainz.org/artist/{mbid}",
                        }
                except Exception as mb_err:
                    mb_info = {"error": str(mb_err)}

                result = {
                    "artist_name": artist_name,
                    "artist_id": artist_id,
                    "album_count": len(parsed),
                    "albums": parsed,
                    "musicbrainz": mb_info,
                }
            except Exception as e:
                error = f"API error: {e}"

        return render_template("debug_artist.html", artist_input=artist_input, result=result, error=error, version=core.get_version())

    # --- Status / health API -----------------------------------------------------

    @app.route("/status")
    def status():
        state = core.load_state()
        if core.clear_expired_rate_limits(state):
            core.save_state(state)
        return jsonify({
            "connected": core.is_connected(),
            "scan_running": core.run_lock.locked(),
            "reorder_running": core.reorder_lock.locked(),
            "in_progress": state.in_progress.to_dict() if state.in_progress else None,
            "rate_limits": dict(state.rate_limits),
            "known_albums_count": len(state.known_albums),
            "logs": core.get_recent_logs()[-40:],
        })

    @app.route("/healthz")
    def healthz():
        """Lightweight liveness probe."""
        return jsonify({"ok": True}), 200

    @app.route("/readyz")
    def readyz():
        """Readiness probe: ready once Spotify credentials are configured."""
        ready = bool(cfg().get("spotify_client_id"))
        return jsonify({"ready": ready}), (200 if ready else 503)

    _start_scheduler()
    return app


# --- Scheduler ---------------------------------------------------------------

def _start_scheduler():
    if os.environ.get("RUN_SCHEDULER", "1") != "1":
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    c = cfg()
    cron_expr = c["cron_schedule"]
    try:
        minute, hour, day, month, dow = _validate_cron_schedule(cron_expr)
    except ValueError:
        core.log(f"Invalid cron schedule {cron_expr!r}; using default 0 6 * * *")
        minute, hour, day, month, dow = "0", "6", "*", "*", "*"

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: core.run_scan(
            days=cfg()["days_lookback"],
            interval_days=cfg()["interval_days"],
            min_request_interval=cfg()["min_request_interval"],
        ),
        trigger=CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow),
    )
    scheduler.start()
    core.log(f"Scheduler started (cron: {cron_expr} UTC)")


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)

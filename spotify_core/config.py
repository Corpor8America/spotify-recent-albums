"""Configuration file handling and version lookup."""

import os
import secrets


CHECK_INTERVAL_DAYS = int(os.environ.get("INTERVAL_DAYS", "3"))
DEFAULT_DAYS_LOOKBACK = int(os.environ.get("DAYS_LOOKBACK", "365"))


def default_config():
    return {
        "spotify_client_id": "",
        "spotify_client_secret": "",
        "spotify_playlist_id": "",
        "interval_days": CHECK_INTERVAL_DAYS,
        "min_request_interval": float(os.environ.get("MIN_REQUEST_INTERVAL", "10")),
        "days_lookback": DEFAULT_DAYS_LOOKBACK,
        "cron_schedule": os.environ.get("CRON_SCHEDULE", "0 6 * * *"),
        "public_base_url": os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        "flask_secret_key": "",
        "musicbrainz_active_refresh_days": int(os.environ.get("MUSICBRAINZ_ACTIVE_REFRESH_DAYS", "30")),
        "verbose_logging": os.environ.get("VERBOSE_LOGGING", "false").lower() == "true",
        "musicbrainz_priority_scan": os.environ.get("MUSICBRAINZ_PRIORITY_SCAN", "false").lower() == "true",
    }


def load_config(ctx):
    config = default_config()
    saved = ctx.store.load_config()
    if saved:
        config.update(saved)
    if not config["flask_secret_key"]:
        config["flask_secret_key"] = secrets.token_hex(32)
        save_config(ctx, config)
    return config


def save_config(ctx, config):
    ctx.store.save_config(config)


def is_configured(ctx):
    cfg = load_config(ctx)
    return bool(cfg.get("spotify_client_id")) and bool(cfg.get("spotify_client_secret"))


def get_version(ctx):
    try:
        return ctx.version_file.read_text().strip()
    except OSError:
        return "unknown"

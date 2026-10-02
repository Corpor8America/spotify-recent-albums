"""Album-name exclusion filters, release-date parsing, and the album
expiry windows (playlist cutoff / expired / retention)."""

import re
from datetime import datetime, timedelta

# Shortest window an album spends in the Expired stage. Retention is twice the
# configured age limit, but a very small (or zero) limit would otherwise
# collapse the stage and drop past albums on the first prune.
MIN_RETENTION_DAYS = 30

PAREN_PATTERN = re.compile(r"(?:\(.*?\)|\[.*?\])\s*$")


def is_auto_excluded(album_name):
    return bool(PAREN_PATTERN.search(album_name.strip()))


def is_effectively_excluded(album):
    """True if the album should not appear in reports or the playlist.

    Accepts an ``Album`` model or a legacy dict (``manual_override`` wins
    over ``auto_excluded``).
    """
    override = album.get("manual_override") if isinstance(album, dict) else album.manual_override
    if override is not None:
        return override
    auto = album.get("auto_excluded", False) if isinstance(album, dict) else album.auto_excluded
    return bool(auto)


def is_promoted(album):
    """True if the user explicitly asked for this album to stay in the
    playlist (manual_override is False)."""
    override = album.get("manual_override") if isinstance(album, dict) else album.manual_override
    return override is False


def parse_release_date(date_str):
    """Parse Spotify's release_date precision formats (Y / Y-m / Y-m-d)."""
    if not date_str:
        return None
    parts = date_str.split("-")
    if len(parts) == 3:
        return datetime.strptime(date_str, "%Y-%m-%d")
    elif len(parts) == 2:
        return datetime.strptime(date_str, "%Y-%m")
    elif len(parts) == 1:
        return datetime.strptime(date_str, "%Y")
    return None


# --- Expiry windows ------------------------------------------------------------
#
# An album's stage is derived from its release date and never stored, so
# changing days_lookback re-buckets every album. Three stages:
#
#   active    release_date newer than the cutoff      kept in the playlist
#   expired   inside retention, older than cutoff     listed, not in playlist
#   retired   older than the retention cutoff         dropped from state


def retention_days(days_lookback):
    """How long an album lingers in Expired: twice the age limit, floored so
    the Expired stage can never be skipped entirely."""
    return max(days_lookback * 2, MIN_RETENTION_DAYS)


def is_past_retention(album, days_lookback, now=None):
    """True once an album's retention window has closed and it should be
    dropped from state entirely. Promoted albums count too -- promotion buys
    time, not permanence."""
    release_date = parse_release_date(album.release_date)
    if release_date is None:
        return False
    now = now if now is not None else datetime.now()
    return release_date < now - timedelta(days=retention_days(days_lookback))


def is_expired(album, days_lookback, now=None):
    """True while an album sits in the Expired stage: past the playlist cutoff
    but still inside the retention window."""
    release_date = parse_release_date(album.release_date)
    if release_date is None:
        return False
    now = now if now is not None else datetime.now()
    return (now - timedelta(days=retention_days(days_lookback)) <= release_date
            < now - timedelta(days=days_lookback))


def is_aged_out(album, days_lookback, now=None):
    """True when an album should leave the playlist because it aged out or was
    excluded -- unless the user promoted it back in."""
    if is_effectively_excluded(album):
        return True
    release_date = parse_release_date(album.release_date)
    if release_date is None:
        return False
    now = now if now is not None else datetime.now()
    return release_date < now - timedelta(days=days_lookback) and not is_promoted(album)

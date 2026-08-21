"""Album-name exclusion filters and release-date parsing."""

import re
from datetime import datetime

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

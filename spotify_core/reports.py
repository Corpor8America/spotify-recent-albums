"""Report queries over known albums (typed Album results)."""

from datetime import datetime, timedelta

from .filters import is_effectively_excluded, parse_release_date


def get_report_albums(state, days):
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for album in state.known_albums.values():
        if is_effectively_excluded(album):
            continue
        release_date = parse_release_date(album.release_date)
        if release_date is None or release_date >= cutoff:
            result.append(album)

    def _sort_key(a):
        d = parse_release_date(a.release_date)
        return d if d is not None else datetime.min

    result.sort(key=_sort_key, reverse=True)
    return result


def get_excluded_albums(state):
    result = [a for a in state.known_albums.values() if is_effectively_excluded(a)]
    result.sort(key=lambda a: a.release_date or "", reverse=True)
    return result


def get_upcoming_albums(state):
    today = datetime.now().date()
    result = []
    for album in state.known_albums.values():
        if is_effectively_excluded(album):
            continue
        release_date = parse_release_date(album.release_date)
        if release_date is None:
            continue
        if release_date.date() > today:
            result.append(album)
    result.sort(key=lambda a: parse_release_date(a.release_date))
    return result

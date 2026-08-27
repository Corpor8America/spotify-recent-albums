"""Report queries over known albums (typed Album results)."""

from datetime import datetime, timedelta

from .filters import is_effectively_excluded, parse_release_date
from .models import Album


def get_report_albums(state, days):
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for album in state.known_albums.values():
        if is_effectively_excluded(album):
            continue
        release_date = parse_release_date(album.release_date)
        if release_date is None or release_date >= cutoff:
            result.append(album)

    # Merge MusicBrainz upcoming albums at the top
    mb_albums = []
    for mb in state.musicbrainz_upcoming.values():
        mb_album = Album(
            id=f"mb_{mb.id}",
            name=mb.name,
            artist=mb.artist,
            artist_id=mb.artist_id,
            album_type="album",
            release_date=mb.release_date,
            url=f"https://musicbrainz.org/release-group/{mb.id}",
            total_tracks=0,
            first_seen=mb.first_seen,
            added_to_playlist=False,
        )
        mb_albums.append(mb_album)

    # Combine: MB albums first, then Spotify albums sorted by date
    def _sort_key(a):
        d = parse_release_date(a.release_date)
        return d if d is not None else datetime.min

    result.sort(key=_sort_key, reverse=True)
    return mb_albums + result


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

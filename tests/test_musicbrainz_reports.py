"""Unit tests for MusicBrainz merge in spotify_core.reports."""

import unittest
from datetime import datetime, timedelta

from spotify_core.models import Album, MusicBrainzAlbum, State
from spotify_core.reports import get_report_albums


def _make_album(album_id, name, release_date):
    return Album(
        id=album_id, name=name, artist="Artist", artist_id="art1", album_type="album",
        release_date=release_date, url=f"https://open.spotify.com/album/{album_id}",
        total_tracks=10, first_seen="2026-08-01T00:00:00+00:00",
    )


def _make_mb_album(rg_id, name, artist="MB Artist", artist_id="art-mb", release_date="2099-06-01"):
    return MusicBrainzAlbum(
        id=rg_id, name=name, artist=artist, artist_id=artist_id,
        release_date=release_date, first_seen="2026-08-01T00:00:00+00:00",
    )


class ReportAlbumsMusicBrainzTests(unittest.TestCase):

    def test_includes_mb_upcoming_albums(self):
        state = State(
            known_albums={"a1": _make_album("a1", "Recent", "2026-07-01")},
            musicbrainz_upcoming={"rg-1": _make_mb_album("rg-1", "Upcoming")},
        )
        result = get_report_albums(state, 365)
        names = [a.name for a in result]
        self.assertIn("Upcoming", names)
        self.assertIn("Recent", names)

    def test_mb_albums_prepended_before_spotify(self):
        state = State(
            known_albums={"a1": _make_album("a1", "Recent", "2026-07-01")},
            musicbrainz_upcoming={"rg-1": _make_mb_album("rg-1", "Upcoming")},
        )
        result = get_report_albums(state, 365)
        self.assertEqual(result[0].name, "Upcoming")
        self.assertEqual(result[1].name, "Recent")

    def test_mb_album_id_prefixed_with_mb(self):
        state = State(
            musicbrainz_upcoming={"rg-1": _make_mb_album("rg-1", "Upcoming")},
        )
        result = get_report_albums(state, 365)
        self.assertTrue(result[0].id.startswith("mb_"))
        self.assertEqual(result[0].id, "mb_rg-1")

    def test_mb_album_url_is_musicbrainz(self):
        state = State(
            musicbrainz_upcoming={"rg-1": _make_mb_album("rg-1", "Upcoming")},
        )
        result = get_report_albums(state, 365)
        self.assertIn("musicbrainz.org", result[0].url)

    def test_mb_album_has_zero_total_tracks(self):
        state = State(
            musicbrainz_upcoming={"rg-1": _make_mb_album("rg-1", "Upcoming")},
        )
        result = get_report_albums(state, 365)
        self.assertEqual(result[0].total_tracks, 0)

    def test_mb_album_not_added_to_playlist(self):
        state = State(
            musicbrainz_upcoming={"rg-1": _make_mb_album("rg-1", "Upcoming")},
        )
        result = get_report_albums(state, 365)
        self.assertFalse(result[0].added_to_playlist)

    def test_multiple_mb_albums_sorted_before_spotify(self):
        state = State(
            known_albums={"a1": _make_album("a1", "Spotify Album", "2026-07-01")},
            musicbrainz_upcoming={
                "rg-1": _make_mb_album("rg-1", "MB Album 1"),
                "rg-2": _make_mb_album("rg-2", "MB Album 2"),
            },
        )
        result = get_report_albums(state, 365)
        # MB albums first, then Spotify
        self.assertEqual(result[0].id, "mb_rg-1")
        self.assertEqual(result[1].id, "mb_rg-2")
        self.assertEqual(result[2].id, "a1")


if __name__ == "__main__":
    unittest.main()

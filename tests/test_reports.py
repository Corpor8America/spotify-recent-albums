import unittest
from datetime import datetime, timedelta

from spotify_core.models import Album, State
from spotify_core.reports import get_excluded_albums, get_report_albums, get_upcoming_albums


def make_album(album_id, name, release_date, auto_excluded=False, manual_override=None):
    return Album(
        id=album_id, name=name, artist="Artist", artist_id="art1", album_type="album",
        release_date=release_date, url=f"https://open.spotify.com/album/{album_id}",
        total_tracks=10, first_seen="2026-08-01T00:00:00+00:00",
        auto_excluded=auto_excluded, manual_override=manual_override,
    )


def state_with(*albums):
    return State(known_albums={a.id: a for a in albums})


class GetReportAlbumsTests(unittest.TestCase):
    def test_filters_excluded_and_sorts(self):
        state = state_with(
            make_album("a1", "Recent", "2026-07-01"),
            make_album("a2", "Old", "2020-01-01"),
            make_album("a3", "Excluded", "2026-06-01", auto_excluded=True),
        )
        result = get_report_albums(state, 365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Recent")

    def test_manual_override_included(self):
        state = state_with(
            make_album("a1", "Live (Live)", "2026-07-01", auto_excluded=True, manual_override=False),
        )
        result = get_report_albums(state, 365)
        self.assertEqual(len(result), 1)

    def test_includes_id_field(self):
        state = state_with(make_album("a1", "Recent", "2026-07-01"))
        result = get_report_albums(state, 365)
        self.assertEqual(result[0].id, "a1")


class GetExcludedAlbumsTests(unittest.TestCase):
    def test_returns_only_excluded(self):
        state = state_with(
            make_album("a1", "Good", "2026-07-01"),
            make_album("a2", "Bad (Live)", "2026-06-01", auto_excluded=True),
        )
        result = get_excluded_albums(state)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "a2")


class GetUpcomingAlbumsTests(unittest.TestCase):
    def _future(self, days):
        return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    def test_returns_future_albums_only(self):
        state = state_with(
            make_album("a1", "Future", self._future(30)),
            make_album("a2", "Past", "2020-01-01"),
            make_album("a3", "Today", datetime.now().strftime("%Y-%m-%d")),
        )
        result = get_upcoming_albums(state)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Future")

    def test_excludes_excluded_albums(self):
        state = state_with(
            make_album("a1", "Future (Live)", self._future(10), auto_excluded=True),
            make_album("a2", "Future", self._future(20)),
        )
        result = get_upcoming_albums(state)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Future")

    def test_manual_override_not_excluded(self):
        state = state_with(
            make_album("a1", "Future (Live)", self._future(10), auto_excluded=True, manual_override=False),
        )
        result = get_upcoming_albums(state)
        self.assertEqual(len(result), 1)

    def test_sorts_soonest_first(self):
        state = state_with(
            make_album("a1", "Far", self._future(60)),
            make_album("a2", "Soon", self._future(5)),
        )
        result = get_upcoming_albums(state)
        self.assertEqual([a.name for a in result], ["Soon", "Far"])


if __name__ == "__main__":
    unittest.main()

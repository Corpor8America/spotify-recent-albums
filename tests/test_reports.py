import unittest
from datetime import datetime, timedelta

from spotify_core.models import Album, MusicBrainzAlbum, State
from spotify_core.reports import (
    get_excluded_albums,
    get_expired_albums,
    get_report_albums,
    get_upcoming_albums,
)


def make_album(album_id, name, release_date, auto_excluded=False, manual_override=None):
    return Album(
        id=album_id, name=name, artist="Artist", artist_id="art1", album_type="album",
        release_date=release_date, url=f"https://open.spotify.com/album/{album_id}",
        total_tracks=10, first_seen="2026-08-01T00:00:00+00:00",
        auto_excluded=auto_excluded, manual_override=manual_override,
    )


def state_with(*albums):
    return State(known_albums={a.id: a for a in albums})


def _days_ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


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

    def test_musicbrainz_exclusion_is_exposed_to_report(self):
        state = state_with()
        state.musicbrainz_upcoming = {
            "rg1": MusicBrainzAlbum(
                "rg1", "Excluded Future", "Artist", "art1", "2099-01-01", "",
                manual_excluded=True,
            ),
        }
        result = get_report_albums(state, 365)
        self.assertTrue(result[0].manual_override)

    def test_musicbrainz_upcoming_albums_sort_newest_first(self):
        state = state_with(
            make_album("sp1", "Spotify", "2026-09-01"),
        )
        state.musicbrainz_upcoming = {
            "mb-old": MusicBrainzAlbum("mb-old", "Older MB", "Artist", "art1", "2026-10-01", ""),
            "mb-new": MusicBrainzAlbum("mb-new", "Newer MB", "Artist", "art1", "2026-12-01", ""),
        }
        result = get_report_albums(state, 365)
        self.assertEqual(
            [a.name for a in result],
            ["Newer MB", "Older MB", "Spotify"],
        )


class GetExcludedAlbumsTests(unittest.TestCase):
    def test_returns_only_excluded(self):
        state = state_with(
            make_album("a1", "Good", _days_ago(10)),
            make_album("a2", "Bad (Live)", _days_ago(20), auto_excluded=True),
        )
        result = get_excluded_albums(state, 365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "a2")

    def test_expired_albums_are_left_to_the_expired_table(self):
        """An album that is both excluded and expired must not appear twice."""
        state = state_with(
            make_album("recent", "Recent (Live)", _days_ago(20), auto_excluded=True),
            make_album("expired", "Old (Live)", _days_ago(400), auto_excluded=True),
        )
        self.assertEqual([a.id for a in get_excluded_albums(state, 365)], ["recent"])
        self.assertEqual([a.id for a in get_expired_albums(state, 365)], ["expired"])


class GetExpiredAlbumsTests(unittest.TestCase):
    def test_lists_only_albums_inside_the_retention_window(self):
        state = state_with(
            make_album("active", "Active", _days_ago(10)),
            make_album("expired", "Expired", _days_ago(400)),
            make_album("retired", "Retired", _days_ago(900)),
        )
        self.assertEqual([a.id for a in get_expired_albums(state, 365)], ["expired"])

    def test_sorted_oldest_first(self):
        state = state_with(
            make_album("newer", "Newer", _days_ago(400)),
            make_album("older", "Older", _days_ago(600)),
        )
        self.assertEqual([a.id for a in get_expired_albums(state, 365)], ["older", "newer"])

    def test_promoted_albums_remain_listed(self):
        state = state_with(
            make_album("p", "Promoted", _days_ago(400), manual_override=False),
        )
        self.assertEqual([a.id for a in get_expired_albums(state, 365)], ["p"])

    def test_unparseable_release_date_is_never_expired(self):
        state = state_with(make_album("x", "No Date", ""))
        self.assertEqual(get_expired_albums(state, 365), [])

    def test_retention_floor_keeps_the_expired_stage_reachable(self):
        """A tiny lookback must still leave an Expired window, not drop albums."""
        state = state_with(
            make_album("a", "A", _days_ago(5)),
            make_album("b", "B", _days_ago(29)),
            make_album("c", "C", _days_ago(31)),
        )
        # lookback 1 day -> retention floors to 30 days, so "c" is already
        # retired while "a" and "b" are expired (oldest first).
        self.assertEqual([a.id for a in get_expired_albums(state, 1)], ["b", "a"])
        self.assertEqual([a.id for a in get_expired_albums(state, 0)], ["b", "a"])


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

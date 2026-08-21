import unittest
from datetime import datetime

from spotify_core.filters import is_auto_excluded, is_effectively_excluded, parse_release_date


class IsAutoExcludedTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertFalse(is_auto_excluded("Album Name"))

    def test_live(self):
        self.assertTrue(is_auto_excluded("Album Name (Live)"))

    def test_remastered(self):
        self.assertTrue(is_auto_excluded("Album Name (Remastered)"))

    def test_deluxe(self):
        self.assertTrue(is_auto_excluded("Album Name (Deluxe Edition)"))

    def test_trailing_whitespace(self):
        self.assertTrue(is_auto_excluded("Album Name (Live) "))

    def test_mid_string_parenthetical_not_trailing(self):
        self.assertFalse(is_auto_excluded("Song (feat. Artist) - Single"))

    def test_trailing_square_brackets(self):
        self.assertTrue(is_auto_excluded("Album Name [Deluxe Edition]"))

    def test_trailing_square_brackets_whitespace(self):
        self.assertTrue(is_auto_excluded("Album Name [Live] "))

    def test_mid_string_bracket_not_trailing(self):
        self.assertFalse(is_auto_excluded("Song [feat. Artist] - Single"))


class IsEffectivelyExcludedTests(unittest.TestCase):
    def test_auto_excluded_no_override(self):
        album = {"auto_excluded": True, "manual_override": None}
        self.assertTrue(is_effectively_excluded(album))

    def test_auto_excluded_with_override_false(self):
        album = {"auto_excluded": True, "manual_override": False}
        self.assertFalse(is_effectively_excluded(album))

    def test_not_auto_excluded_with_override_true(self):
        album = {"auto_excluded": False, "manual_override": True}
        self.assertTrue(is_effectively_excluded(album))

    def test_not_auto_excluded_no_override(self):
        album = {"auto_excluded": False, "manual_override": None}
        self.assertFalse(is_effectively_excluded(album))

    def test_album_model(self):
        from spotify_core.models import Album

        excluded = Album(id="a", name="x", artist="x", artist_id="x", album_type="album",
                         release_date="2026-01-01", url="", total_tracks=1, first_seen="",
                         auto_excluded=True)
        self.assertTrue(is_effectively_excluded(excluded))
        excluded.manual_override = False
        self.assertFalse(is_effectively_excluded(excluded))


class ParseReleaseDateTests(unittest.TestCase):
    def test_full_date(self):
        self.assertEqual(parse_release_date("2026-07-29"), datetime(2026, 7, 29))

    def test_year_month(self):
        self.assertEqual(parse_release_date("2026-07"), datetime(2026, 7, 1))

    def test_year_only(self):
        self.assertEqual(parse_release_date("2026"), datetime(2026, 1, 1))

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_release_date(""))


if __name__ == "__main__":
    unittest.main()

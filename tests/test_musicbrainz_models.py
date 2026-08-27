"""Unit tests for MusicBrainz-related model serialization roundtrips."""

import unittest

from spotify_core.models import Artist, MusicBrainzAlbum, State


class MusicBrainzAlbumRoundtripTests(unittest.TestCase):

    def test_to_dict_from_dict_roundtrip(self):
        original = MusicBrainzAlbum(
            id="rg-123", name="Test Album", artist="Test Artist",
            artist_id="art-456", release_date="2099-06-01", first_seen="2026-08-01",
        )
        d = original.to_dict()
        restored = MusicBrainzAlbum.from_dict("rg-123", d)
        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.name, original.name)
        self.assertEqual(restored.artist, original.artist)
        self.assertEqual(restored.artist_id, original.artist_id)
        self.assertEqual(restored.release_date, original.release_date)
        self.assertEqual(restored.first_seen, original.first_seen)

    def test_from_dict_defaults(self):
        restored = MusicBrainzAlbum.from_dict("rg-1", {})
        self.assertEqual(restored.id, "rg-1")
        self.assertEqual(restored.name, "")
        self.assertEqual(restored.artist, "")
        self.assertEqual(restored.artist_id, "")
        self.assertEqual(restored.release_date, "")
        self.assertEqual(restored.first_seen, "")


class ArtistMbFieldsRoundtripTests(unittest.TestCase):

    def test_mb_fields_roundtrip(self):
        original = Artist(
            id="a1", name="Test", musicbrainz_id="mb-123",
            mb_active=False, mb_active_checked="2026-08-01T00:00:00",
        )
        d = original.to_dict()
        restored = Artist.from_dict("a1", d)
        self.assertEqual(restored.musicbrainz_id, "mb-123")
        self.assertFalse(restored.mb_active)
        self.assertEqual(restored.mb_active_checked, "2026-08-01T00:00:00")

    def test_mb_fields_defaults(self):
        restored = Artist.from_dict("a1", {"name": "Test"})
        self.assertEqual(restored.musicbrainz_id, "")
        self.assertTrue(restored.mb_active)
        self.assertEqual(restored.mb_active_checked, "")


class StateMusicbrainzUpcomingRoundtripTests(unittest.TestCase):

    def test_roundtrip_preserves_upcoming(self):
        original = State(musicbrainz_upcoming={
            "rg-1": MusicBrainzAlbum(id="rg-1", name="Upcoming", artist="A",
                                     artist_id="a1", release_date="2099-01-01",
                                     first_seen="2026-08-01"),
        })
        d = original.to_dict()
        restored = State.from_dict(d)
        self.assertIn("rg-1", restored.musicbrainz_upcoming)
        self.assertEqual(restored.musicbrainz_upcoming["rg-1"].name, "Upcoming")

    def test_missing_musicbrainz_upcoming_defaults_empty(self):
        restored = State.from_dict({"artists": {}, "known_albums": {}})
        self.assertEqual(restored.musicbrainz_upcoming, {})

    def test_empty_dict_upcoming_defaults_empty(self):
        restored = State.from_dict({
            "artists": {}, "known_albums": {},
            "musicbrainz_upcoming": {},
        })
        self.assertEqual(restored.musicbrainz_upcoming, {})


if __name__ == "__main__":
    unittest.main()

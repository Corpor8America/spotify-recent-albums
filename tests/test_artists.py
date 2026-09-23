"""Tests for followed-artist scheduling and MusicBrainz deferrals."""

import unittest
from datetime import datetime, timedelta, timezone

from spotify_core.artists import get_due_artists
from spotify_core.models import Artist, MusicBrainzAlbum, State


class GetDueArtistsTests(unittest.TestCase):

    def _artist(self, artist_id="a1", name="A"):
        return {"id": artist_id, "name": name}

    def test_future_mb_release_does_not_crash_or_make_artist_due(self):
        state = State(
            artists={"a1": Artist(id="a1", name="A", musicbrainz_id="mb-123")},
            musicbrainz_upcoming={
                "rg-1": MusicBrainzAlbum(
                    id="rg-1", name="Future Album", artist="A", artist_id="a1",
                    release_date="2099-12-31",
                    first_seen=datetime.now(timezone.utc).isoformat(),
                ),
            },
        )
        self.assertEqual(get_due_artists([self._artist()], state, 3), [])

    def test_expired_mb_release_no_longer_defers_empty_last_checked_artist(self):
        state = State(
            artists={"a1": Artist(id="a1", name="A", musicbrainz_id="mb-123")},
            musicbrainz_upcoming={
                "rg-1": MusicBrainzAlbum(
                    id="rg-1", name="Released Album", artist="A", artist_id="a1",
                    release_date="2020-01-01",
                    first_seen=datetime.now(timezone.utc).isoformat(),
                ),
            },
        )
        self.assertEqual(get_due_artists([self._artist()], state, 3), [self._artist()])

    def test_empty_last_checked_without_mb_deferral_is_due(self):
        state = State(artists={
            "a1": Artist(id="a1", name="A", musicbrainz_id="mb-123"),
        })
        self.assertEqual(get_due_artists([self._artist()], state, 3), [self._artist()])

    def test_deferred_artist_does_not_displace_normal_artist_in_fallback(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        artists = [self._artist("a1", "Deferred"), self._artist("a2", "Normal")]
        state = State(
            artists={
                "a1": Artist(id="a1", name="Deferred", musicbrainz_id="mb-1"),
                "a2": Artist(id="a2", name="Normal", last_checked=old),
            },
            musicbrainz_upcoming={
                "rg-1": MusicBrainzAlbum(
                    id="rg-1", name="Future Album", artist="Deferred", artist_id="a1",
                    release_date="2099-12-31",
                    first_seen=datetime.now(timezone.utc).isoformat(),
                ),
            },
        )
        due = get_due_artists(artists, state, 30)
        self.assertEqual([a["id"] for a in due], ["a2"])


if __name__ == "__main__":
    unittest.main()

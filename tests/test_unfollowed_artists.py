import unittest
from unittest.mock import patch

import spotify_core as core
from spotify_core.models import Album, Artist, MusicBrainzAlbum, State
from tests.support import ContextTestCase


def make_album(album_id, artist_id, added=False, track_uris=None):
    return Album(
        id=album_id,
        name=album_id,
        artist=artist_id,
        artist_id=artist_id,
        album_type="album",
        release_date="2026-09-01",
        url="",
        total_tracks=1,
        first_seen="",
        added_to_playlist=added,
        track_uris=list(track_uris or []),
    )


class UnfollowedArtistCleanupTests(ContextTestCase):
    def test_removes_artist_albums_mb_entries_and_playlist_tracks(self):
        state = State(
            artists={
                "followed": Artist(id="followed", name="Followed"),
                "unfollowed": Artist(id="unfollowed", name="Unfollowed"),
            },
            known_albums={
                "old": make_album("old", "unfollowed", added=True, track_uris=["old-track"]),
                "keep": make_album("keep", "followed", added=True, track_uris=["keep-track"]),
            },
            musicbrainz_upcoming={
                "rg1": MusicBrainzAlbum(
                    id="rg1", name="Upcoming", artist="Unfollowed",
                    artist_id="unfollowed", release_date="2026-10-01", first_seen="",
                ),
            },
        )

        with patch.object(core.playlists, "remove_tracks_from_playlist") as remove:
            count = core.playlists.remove_unfollowed_artists(
                self.ctx, "token", state, {"followed"}, "playlist"
            )

        self.assertEqual(count, 1)
        remove.assert_called_once_with(
            self.ctx, "token", "playlist", ["old-track"], state
        )
        self.assertNotIn("unfollowed", state.artists)
        self.assertNotIn("old", state.known_albums)
        self.assertNotIn("rg1", state.musicbrainz_upcoming)
        self.assertIn("followed", state.artists)
        self.assertIn("keep", state.known_albums)

    def test_shared_track_is_preserved(self):
        state = State(
            known_albums={
                "old": make_album("old", "unfollowed", added=True,
                                  track_uris=["shared", "old-only"]),
                "keep": make_album("keep", "followed", added=True,
                                   track_uris=["shared"]),
            }
        )

        with patch.object(core.playlists, "remove_tracks_from_playlist") as remove:
            core.playlists.remove_unfollowed_artists(
                self.ctx, "token", state, {"followed"}, "playlist"
            )

        remove.assert_called_once_with(
            self.ctx, "token", "playlist", ["old-only"], state
        )

    def test_no_playlist_still_removes_state(self):
        state = State(
            artists={"unfollowed": Artist(id="unfollowed", name="Unfollowed")},
            known_albums={"old": make_album("old", "unfollowed", added=True, track_uris=["old"])},
        )

        with patch.object(core.playlists, "remove_tracks_from_playlist") as remove:
            core.playlists.remove_unfollowed_artists(
                self.ctx, "token", state, set(), None
            )

        remove.assert_not_called()
        self.assertEqual(state.artists, {})
        self.assertEqual(state.known_albums, {})

    def test_playlist_failure_keeps_state_for_retry(self):
        state = State(
            artists={"unfollowed": Artist(id="unfollowed", name="Unfollowed")},
            known_albums={"old": make_album("old", "unfollowed", added=True, track_uris=["old"])},
        )

        with patch.object(
            core.playlists, "remove_tracks_from_playlist",
            side_effect=RuntimeError("playlist failure"),
        ):
            with self.assertRaises(RuntimeError):
                core.playlists.remove_unfollowed_artists(
                    self.ctx, "token", state, set(), "playlist"
                )

        self.assertIn("unfollowed", state.artists)
        self.assertIn("old", state.known_albums)

    def test_scan_passes_current_followed_artist_ids_to_cleanup(self):
        artists = [{"id": "followed", "name": "Followed"}]
        with patch.object(core.scan, "get_access_token", return_value="tok"),              patch.object(core.scan, "get_followed_artists", return_value=artists),              patch.object(core.scan, "remove_unfollowed_artists") as cleanup,              patch.object(core.scan, "_plan_artists", return_value=([], set(), set())):
            core.run_scan(days=365, interval_days=3, min_request_interval=0)

        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args.args[3], {"followed"})


if __name__ == "__main__":
    unittest.main()

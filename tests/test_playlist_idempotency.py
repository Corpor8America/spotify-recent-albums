from unittest.mock import patch

import spotify_core as core
from spotify_core.models import State
from tests.support import ContextTestCase


class AddTracksIdempotencyTests(ContextTestCase):
    def test_existing_tracks_are_not_added_again(self):
        existing = ["spotify:track:already", "spotify:track:other"]
        requested = ["spotify:track:already", "spotify:track:new"]

        with patch.object(core.playlists, "get_playlist_track_uris", return_value=existing), \
             patch.object(core.playlists, "spotify_request") as request:
            added = core.add_tracks_to_playlist(
                "token", "playlist", requested, State())

        self.assertEqual(added, ["spotify:track:new"])
        request.assert_called_once_with(
            self.ctx,
            "POST",
            "token",
            "https://api.spotify.com/v1/playlists/playlist/items",
            unittest.mock.ANY,
            json_data={"uris": ["spotify:track:new"]},
        )

    def test_duplicate_requested_tracks_are_only_added_once(self):
        requested = ["spotify:track:new", "spotify:track:new", "spotify:track:other"]

        with patch.object(core.playlists, "get_playlist_track_uris", return_value=[]), \
             patch.object(core.playlists, "spotify_request") as request:
            added = core.add_tracks_to_playlist(
                "token", "playlist", requested, State())

        self.assertEqual(added, ["spotify:track:new", "spotify:track:other"])
        self.assertEqual(request.call_count, 1)

    def test_all_existing_tracks_causes_no_write(self):
        requested = ["spotify:track:a", "spotify:track:b"]

        with patch.object(core.playlists, "get_playlist_track_uris", return_value=requested), \
             patch.object(core.playlists, "spotify_request") as request:
            added = core.add_tracks_to_playlist(
                "token", "playlist", requested, State())

        self.assertEqual(added, [])
        request.assert_not_called()

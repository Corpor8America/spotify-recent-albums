from unittest.mock import ANY, patch

import spotify_core as core
from spotify_core.models import ScanProgress, State
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
            ANY,
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

    def test_scan_reuses_playlist_snapshot_for_multiple_album_adds(self):
        state = State(in_progress=ScanProgress(due_ids=["artist"], processed_ids=[]))
        existing = ["spotify:track:already"]

        with patch.object(core.playlists, "get_playlist_track_uris", return_value=existing) as get_playlist, \
             patch.object(core.playlists, "spotify_request") as request, \
             patch.object(core.playlists.state_mod, "save_state"):
            first = core.add_tracks_to_playlist(
                "token", "playlist", ["spotify:track:first"], state)
            second = core.add_tracks_to_playlist(
                "token", "playlist", ["spotify:track:second", "spotify:track:first"], state)

        self.assertEqual(first, ["spotify:track:first"])
        self.assertEqual(second, ["spotify:track:second"])
        get_playlist.assert_called_once_with(self.ctx, "token", "playlist", state)
        self.assertEqual(request.call_count, 2)
        self.assertIn("spotify:track:first", state.in_progress.playlist_track_uris)
        self.assertIn("spotify:track:second", state.in_progress.playlist_track_uris)

    def test_rate_limit_resume_can_reuse_persisted_playlist_snapshot(self):
        snapshot = ["spotify:track:already", "spotify:track:added_before_limit"]
        state = State(
            in_progress=ScanProgress(
                due_ids=["artist"],
                processed_ids=[],
                playlist_track_uris=snapshot,
            )
        )

        with patch.object(core.playlists, "get_playlist_track_uris") as get_playlist, \
             patch.object(core.playlists, "spotify_request") as request, \
             patch.object(core.playlists.state_mod, "save_state"):
            added = core.add_tracks_to_playlist(
                "token", "playlist", ["spotify:track:added_before_limit", "spotify:track:after_limit"], state)

        self.assertEqual(added, ["spotify:track:after_limit"])
        get_playlist.assert_not_called()
        request.assert_called_once()

    def test_scan_progress_serialization_keeps_playlist_snapshot(self):
        state = State(
            in_progress=ScanProgress(
                due_ids=["artist"],
                processed_ids=["done"],
                playlist_track_uris=["spotify:track:a", "spotify:track:b"],
            )
        )

        restored = State.from_dict(state.to_dict())

        self.assertEqual(restored.in_progress.playlist_track_uris,
                         ["spotify:track:a", "spotify:track:b"])

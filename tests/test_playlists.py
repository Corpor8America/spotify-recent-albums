import unittest
from unittest.mock import patch

import spotify_core as core
from spotify_core.models import Album, State
from tests.support import ContextTestCase


def make_album(album_id, name, release_date, added=False, track_uris=None,
               auto_excluded=False, manual_override=None):
    return Album(
        id=album_id, name=name, artist="Artist", artist_id="art1", album_type="album",
        release_date=release_date, url="", total_tracks=10, first_seen="",
        auto_excluded=auto_excluded, manual_override=manual_override,
        added_to_playlist=added, track_uris=list(track_uris or []),
    )


class PrunePlaylistTests(ContextTestCase):
    def test_removes_aged_out_album_tracks(self):
        state = State(known_albums={
            "alb1": make_album("alb1", "Old Album", "2020-01-01",
                               added=True, track_uris=["spotify:track:a", "spotify:track:b"]),
        })
        calls = []
        with patch.object(core.playlists, "remove_tracks_from_playlist",
                          side_effect=lambda ctx, t, p, uris, s: calls.append(uris)):
            core.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:a", "spotify:track:b"]])
        self.assertFalse(state.known_albums["alb1"].added_to_playlist)

    def test_shared_track_is_not_removed(self):
        state = State(known_albums={
            "old": make_album("old", "Old", "2020-01-01", added=True,
                              track_uris=["spotify:track:shared", "spotify:track:only_old"]),
            "current": make_album("current", "Current", "2026-07-01", added=True,
                                  track_uris=["spotify:track:shared"]),
        })
        calls = []
        with patch.object(core.playlists, "remove_tracks_from_playlist",
                          side_effect=lambda ctx, t, p, uris, s: calls.append(uris)):
            core.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:only_old"]])

    def test_missing_track_uris_falls_back_to_fetch(self):
        state = State(known_albums={
            "alb1": make_album("alb1", "Old Album", "2020-01-01", added=True),
        })
        with patch.object(core.playlists, "get_album_track_uris",
                          return_value=["spotify:track:fetched"]) as fetch_mock, \
             patch.object(core.playlists, "remove_tracks_from_playlist") as remove_mock:
            core.prune_playlist("token", state, 365, "playlist123")
        fetch_mock.assert_called_once()
        remove_mock.assert_called_once_with(self.ctx, "token", "playlist123",
                                            ["spotify:track:fetched"], state)

    def test_no_playlist_id_is_noop(self):
        state = State(known_albums={
            "alb1": make_album("alb1", "X", "2020-01-01", added=True, track_uris=["u"]),
        })
        with patch.object(core.playlists, "remove_tracks_from_playlist") as remove_mock:
            core.prune_playlist("token", state, 365, None)
        remove_mock.assert_not_called()

    def test_not_yet_expired_album_is_untouched(self):
        state = State(known_albums={
            "alb1": make_album("alb1", "X", "2026-07-01", added=True, track_uris=["u"]),
        })
        with patch.object(core.playlists, "remove_tracks_from_playlist") as remove_mock:
            core.prune_playlist("token", state, 365, "playlist123")
        remove_mock.assert_not_called()

    def test_manually_excluded_album_is_pruned(self):
        state = State(known_albums={
            "alb1": make_album("alb1", "Live Album (Live)", "2026-07-01", added=True,
                               track_uris=["spotify:track:x"], manual_override=True),
        })
        calls = []
        with patch.object(core.playlists, "remove_tracks_from_playlist",
                          side_effect=lambda ctx, t, p, uris, s: calls.append(uris)):
            core.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:x"]])
        self.assertFalse(state.known_albums["alb1"].added_to_playlist)

    def test_manually_included_album_is_not_pruned(self):
        state = State(known_albums={
            "alb1": make_album("alb1", "Live Album (Live)", "2026-07-01", added=True,
                               track_uris=["spotify:track:x"],
                               auto_excluded=True, manual_override=False),
        })
        with patch.object(core.playlists, "remove_tracks_from_playlist") as remove_mock:
            core.prune_playlist("token", state, 365, "playlist123")
        remove_mock.assert_not_called()

    def test_shared_track_excluded_and_current(self):
        state = State(known_albums={
            "excluded": make_album("excluded", "Live (Live)", "2026-07-01", added=True,
                                   track_uris=["spotify:track:shared", "spotify:track:only_excluded"],
                                   manual_override=True),
            "current": make_album("current", "Current", "2026-07-01", added=True,
                                  track_uris=["spotify:track:shared"]),
        })
        calls = []
        with patch.object(core.playlists, "remove_tracks_from_playlist",
                          side_effect=lambda ctx, t, p, uris, s: calls.append(uris)):
            core.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:only_excluded"]])


class ReorderPlaylistTests(ContextTestCase):
    def test_reorder_clears_current_playlist_and_rebuilds_from_state(self):
        state = State(known_albums={
            "new": make_album("new", "New", "2026-07-01", added=True,
                              track_uris=["new-1", "new-2"]),
            "old": make_album("old", "Old", "2026-05-01", added=True,
                              track_uris=["old-1", "old-2"]),
        })
        # These stale items must be cleared; only state-backed tracks return.
        current = ["new-1", "old-1", "external", "new-2", "old-2", "new-1"]

        with patch.object(core.playlists, "get_playlist_track_uris", return_value=current), \
             patch.object(core.playlists, "remove_tracks_from_playlist") as remove, \
             patch.object(core.playlists, "add_tracks_to_playlist") as add:
            core.reorder_playlist("token", state, "playlist")

        remove.assert_called_once_with(self.ctx, "token", "playlist", current, state)
        add.assert_called_once_with(
            self.ctx, "token", "playlist",
            ["old-1", "old-2", "new-1", "new-2"], state,
        )


class ReorderPlaylistMultiAlbumTests(ContextTestCase):
    def test_three_albums_sorted_oldest_first(self):
        state = State(known_albums={
            "new": make_album("new", "New", "2026-07-01", added=True,
                              track_uris=["new-1", "new-2"]),
            "mid": make_album("mid", "Mid", "2026-03-15", added=True,
                              track_uris=["mid-1", "mid-2"]),
            "old": make_album("old", "Old", "2025-12-01", added=True,
                              track_uris=["old-1", "old-2"]),
        })

        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["new-1", "mid-1", "old-1"]), \
             patch.object(core.playlists, "remove_tracks_from_playlist"), \
             patch.object(core.playlists, "add_tracks_to_playlist") as add:
            core.reorder_playlist("token", state, "playlist")

        add.assert_called_once_with(
            self.ctx, "token", "playlist",
            ["old-1", "old-2", "mid-1", "mid-2", "new-1", "new-2"], state,
        )

    def test_track_count_preserved_after_reorder(self):
        state = State(known_albums={
            "a": make_album("a", "A", "2026-01-01", added=True,
                            track_uris=["a1", "a2", "a3"]),
            "b": make_album("b", "B", "2026-06-01", added=True,
                            track_uris=["b1", "b2"]),
        })

        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["a1", "b1"]), \
             patch.object(core.playlists, "remove_tracks_from_playlist"), \
             patch.object(core.playlists, "add_tracks_to_playlist") as add:
            core.reorder_playlist("token", state, "playlist")

        added_uris = add.call_args.args[3]
        self.assertEqual(len(added_uris), 5)


class ApplyAlbumOverrideTests(ContextTestCase):
    """Unit tests for the extracted override flow (no playlist configured)."""

    def _seed_album(self, **kwargs):
        core.save_state(State(known_albums={"alb1": make_album("alb1", "Test Album", "2026-07-01", **kwargs)}))

    def test_unknown_album_returns_false(self):
        self.assertFalse(core.apply_album_override("nonexistent", "true"))

    def test_override_true_sets_manual_override(self):
        self._seed_album()
        self.assertTrue(core.apply_album_override("alb1", "true"))
        loaded = core.load_state()
        self.assertTrue(loaded.known_albums["alb1"].manual_override)

    def test_override_false_clears_manual_override(self):
        self._seed_album(manual_override=True)
        self.assertTrue(core.apply_album_override("alb1", "false"))
        loaded = core.load_state()
        self.assertFalse(loaded.known_albums["alb1"].manual_override)

    def test_override_none_value_resets(self):
        self._seed_album(manual_override=True)
        self.assertTrue(core.apply_album_override("alb1", ""))
        loaded = core.load_state()
        self.assertIsNone(loaded.known_albums["alb1"].manual_override)

    def test_exclude_removes_added_album_tracks(self):
        from tests.support import write_token

        self._seed_album(added=True, track_uris=["spotify:track:a"])
        self.write_config({"spotify_playlist_id": "playlist123"})
        write_token(self.ctx, "refresh")
        with patch.object(core.auth, "get_access_token", return_value="tok"), \
             patch.object(core.playlists, "remove_tracks_from_playlist") as remove:
            self.assertTrue(core.apply_album_override("alb1", "true"))
        remove.assert_called_once()
        entry = core.load_state().known_albums["alb1"]
        self.assertFalse(entry.added_to_playlist)
        self.assertEqual(entry.track_uris, [])

    def test_include_adds_back_tracks(self):
        from tests.support import write_token

        self._seed_album(auto_excluded=True)
        self.write_config({"spotify_playlist_id": "playlist123"})
        write_token(self.ctx, "refresh")
        with patch.object(core.auth, "get_access_token", return_value="tok"), \
             patch.object(core.playlists, "get_album_track_uris",
                          return_value=["spotify:track:a"]) as fetch, \
             patch.object(core.playlists, "add_tracks_to_playlist") as add:
            self.assertTrue(core.apply_album_override("alb1", "false"))
        fetch.assert_called_once()
        add.assert_called_once()
        entry = core.load_state().known_albums["alb1"]
        self.assertTrue(entry.added_to_playlist)
        self.assertEqual(entry.track_uris, ["spotify:track:a"])


class PlaylistOrderIsStaleTests(ContextTestCase):
    """Tests for playlist_order_is_stale."""

    def test_returns_false_when_order_matches(self):
        state = State(known_albums={
            "a1": make_album("a1", "Old", "2026-01-01", added=True,
                             track_uris=["old-1", "old-2"]),
            "a2": make_album("a2", "New", "2026-06-01", added=True,
                             track_uris=["new-1", "new-2"]),
        })
        # Current order matches release-date order (oldest first)
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["old-1", "old-2", "new-1", "new-2"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertFalse(result)

    def test_returns_true_when_tracks_swapped(self):
        state = State(known_albums={
            "a1": make_album("a1", "Old", "2026-01-01", added=True,
                             track_uris=["old-1"]),
            "a2": make_album("a2", "New", "2026-06-01", added=True,
                             track_uris=["new-1"]),
        })
        # Current order has new before old (wrong)
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["new-1", "old-1"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertTrue(result)

    def test_returns_true_when_tracks_within_album_swapped(self):
        state = State(known_albums={
            "a1": make_album("a1", "Album", "2026-01-01", added=True,
                             track_uris=["a-1", "a-2"]),
        })
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["a-2", "a-1"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertTrue(result)

    def test_returns_true_when_seen_album_is_missing_tracks(self):
        state = State(known_albums={
            "a1": make_album("a1", "Album", "2026-01-01", added=True,
                             track_uris=["a-1", "a-2"]),
        })
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["a-1"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertTrue(result)

    def test_ignores_stray_uris(self):
        state = State(known_albums={
            "a1": make_album("a1", "Album", "2026-01-01", added=True,
                             track_uris=["a-1"]),
        })
        # Stray URI "external-track" is not in any known album
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["a-1", "external-track"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertFalse(result)

    def test_ignores_missing_albums_in_playlist(self):
        state = State(known_albums={
            "a1": make_album("a1", "Album1", "2026-01-01", added=True,
                             track_uris=["a1-1"]),
            "a2": make_album("a2", "Album2", "2026-06-01", added=True,
                             track_uris=["a2-1"]),
        })
        # a2 is not in the playlist currently
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["a1-1"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertFalse(result)

    def test_excluded_albums_not_in_either_sequence(self):
        state = State(known_albums={
            "a1": make_album("a1", "Normal", "2026-01-01", added=True,
                             track_uris=["n-1"]),
            "a2": make_album("a2", "Excluded (Live)", "2026-06-01", added=True,
                             track_uris=["e-1"], manual_override=True),
        })
        with patch.object(core.playlists, "get_playlist_track_uris",
                          return_value=["n-1"]):
            result = core.playlists.playlist_order_is_stale(
                self.ctx, "token", state, "playlist")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()

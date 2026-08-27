import json
import os
import stat
import unittest

import spotify_core as core
from spotify_core.models import Artist, ScanProgress, State
from tests.support import ContextTestCase


class TokenFilePermissionsTests(ContextTestCase):
    """Regression: the token file must stay readable by non-root users.
    CI reads the bind-mounted file back as an unprivileged runner while
    the app container writes it as root -- a 0600 mode breaks that."""

    @unittest.skipUnless(os.name == "posix", "file modes only meaningful on POSIX")
    def test_saved_token_file_is_not_owner_only(self):
        core.save_refresh_token("secret")
        mode = stat.S_IMODE(os.stat(self.ctx.store.token_file).st_mode)
        self.assertTrue(mode & 0o044, f"token file not readable by others: {oct(mode)}")


class StateFileTests(ContextTestCase):
    def test_load_returns_defaults_when_missing(self):
        state = core.load_state()
        self.assertEqual(state.artists, {})
        self.assertEqual(state.known_albums, {})
        self.assertIsNone(state.in_progress)
        self.assertEqual(state.rate_limits, {})

    def test_save_and_load_roundtrip(self):
        original = State(
            artists={"a1": Artist(id="a1", name="Test", last_checked="2026-01-01T00:00:00")},
        )
        core.save_state(original)
        loaded = core.load_state()
        self.assertEqual(loaded.artists["a1"].name, "Test")
        self.assertEqual(loaded.artists["a1"].last_checked, "2026-01-01T00:00:00")

    def test_roundtrip_matches_legacy_json_shape(self):
        original = State(
            artists={"a1": Artist(id="a1", name="Test", last_checked="2026-01-01T00:00:00")},
            rate_limits={"GET /me/following": 123},
        )
        core.save_state(original)
        raw = json.loads((self.tmp_path / "spotify-state.json").read_text())
        self.assertEqual(set(raw.keys()), {"artists", "known_albums", "in_progress", "rate_limits", "musicbrainz_upcoming"})
        self.assertEqual(set(raw["artists"]["a1"].keys()), {"name", "last_checked", "scanned_with", "musicbrainz_id", "mb_active", "mb_active_checked"})

    def test_clear_expired_rate_limits_removes_only_past_entries(self):
        state = State(rate_limits={"expired": 99, "future": 101})
        changed = core.clear_expired_rate_limits(state, now=100)
        self.assertTrue(changed)
        self.assertEqual(state.rate_limits, {"future": 101})

    def test_clear_expired_rate_limits_reports_no_change(self):
        state = State(rate_limits={"future": 101})
        changed = core.clear_expired_rate_limits(state, now=100)
        self.assertFalse(changed)
        self.assertEqual(state.rate_limits, {"future": 101})


class UpdateStateTests(ContextTestCase):
    def test_mutator_applied_and_persisted(self):
        def add_artist(state):
            state.artists["a1"] = Artist(id="a1", name="Test")
            return state
        result = core.update_state(add_artist)
        self.assertEqual(result.artists["a1"].name, "Test")
        loaded = core.load_state()
        self.assertEqual(loaded.artists["a1"].name, "Test")

    def test_returning_none_leaves_file_untouched(self):
        core.save_state(State())

        def noop(state):
            return None
        core.update_state(noop)
        loaded = core.load_state()
        self.assertEqual(loaded.artists, {})


class CancelScanTests(ContextTestCase):
    def test_cancel_sets_event_and_clears_in_progress(self):
        core.save_state(State(in_progress=ScanProgress(due_ids=["a1"], processed_ids=[])))
        core.cancel_scan()
        loaded = core.load_state()
        self.assertIsNone(loaded.in_progress)
        self.assertTrue(core._cancel_event.is_set())
        core._cancel_event.clear()


if __name__ == "__main__":
    unittest.main()

"""Service layer between Flask routes and spotify_core.

Routes stay thin dispatchers; business orchestration lives here. All
calls go through the ``core.*`` wrappers so tests can patch either side
(e.g. ``patch("app.core.start_scan")``).
"""

import threading

import spotify_core as core


class ScanService:
    """Scan triggers and status for the dashboard."""

    def trigger_now(self, cfg):
        """Start a background scan using the given config. Returns True if
        it was started."""
        return core.start_scan(
            days=cfg["days_lookback"],
            interval_days=cfg["interval_days"],
            min_request_interval=cfg["min_request_interval"],
        )

    def cancel(self):
        core.cancel_scan()

    def is_running(self):
        return core.run_lock.locked()


class PlaylistService:

    def apply_override(self, album_id, value):
        """Apply a manual include/exclude override. Returns False when the
        album is unknown."""
        return core.apply_album_override(album_id, value)

    def create(self, name):
        """Create a playlist on the connected account and save its id as
        the sync target. Returns (ok, error_message)."""
        cfg = core.load_config()
        client_id = cfg["spotify_client_id"]
        client_secret = cfg["spotify_client_secret"]
        refresh_token = core.load_refresh_token()
        if not all([client_id, client_secret, refresh_token]):
            return False, "Not connected to Spotify"

        try:
            token = core.get_access_token(client_id, client_secret, refresh_token)
            playlist_id = core.create_playlist(token, name)
        except Exception as e:
            core.log(f"Playlist creation failed: {e}")
            return False, f"Playlist creation failed: {e}"

        cfg["spotify_playlist_id"] = playlist_id
        core.save_config(cfg)
        core.log(f"Created playlist {name!r} ({playlist_id}); set as sync target.")
        return True, None

    def reorder_async(self):
        """Kick off a background reorder; no-op if one is already running."""
        if not core.reorder_lock.acquire(blocking=False):
            core.log("Reorder already in progress.")
            return
        threading.Thread(target=self._reorder, kwargs={"lock_held": True}, daemon=True).start()

    def _reorder(self, lock_held=False):
        if not lock_held and not core.reorder_lock.acquire(blocking=False):
            core.log("Reorder already in progress.")
            return
        try:
            # Reorder is destructive (delete-all then re-add), so it must not
            # overlap a scan's playlist additions/pruning. Wait for any active
            # scan to finish; run_lock also prevents a new scan from starting.
            core.run_lock.acquire(blocking=True)
            try:
                cfg = core.load_config()
                core.get_context().rate_limiter.min_interval_seconds = cfg["min_request_interval"]
                client_id = cfg["spotify_client_id"]
                client_secret = cfg["spotify_client_secret"]
                refresh_token = core.load_refresh_token()
                if not all([client_id, client_secret, refresh_token]):
                    core.log("Cannot reorder -- not connected.")
                    return
                token = core.get_access_token(client_id, client_secret, refresh_token)
                state = core.load_state()
                playlist_id = cfg["spotify_playlist_id"]
                core.reorder_playlist(token, state, playlist_id)
            finally:
                core.run_lock.release()
        finally:
            core.reorder_lock.release()

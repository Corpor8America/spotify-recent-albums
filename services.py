"""Service layer between Flask routes and spotify_core.

Routes stay thin dispatchers; business orchestration lives here. All
calls go through the ``core.*`` wrappers so tests can patch either side
(e.g. ``patch("app.core.start_scan")``).
"""

import threading
from concurrent.futures import ThreadPoolExecutor

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
    """Playlist mutations triggered from the web UI.

    Mutating methods return ``(status, error)``: ``(None, None)`` on success,
    otherwise an HTTP status and the message to show. Statuses live here so
    routes stay thin dispatchers.
    """

    def __init__(self):
        self._promote_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="album-promote")
        self._promote_status = {}
        self._promote_status_lock = threading.Lock()

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

    def promote_expired_async(self, album_id):
        """Queue an expired-album promotion and return immediately."""
        if not core.is_connected():
            return 400, {"status": "failed", "message": "Not connected to Spotify"}
        if self._expired_album(album_id) is None:
            return 404, {"status": "failed", "message": "Unknown expired album"}

        with self._promote_status_lock:
            current = self._promote_status.get(album_id)
            if current and current["status"] in {"queued", "running"}:
                return 202, dict(current)
            status = {"status": "queued"}
            self._promote_status[album_id] = status

        self._promote_executor.submit(self._run_promote, album_id)
        return 202, dict(status)

    def promote_status(self, album_id):
        with self._promote_status_lock:
            status = self._promote_status.get(album_id)
            return dict(status) if status else None

    def promote_statuses(self):
        with self._promote_status_lock:
            return {album_id: dict(status) for album_id, status in self._promote_status.items()}

    def _set_promote_status(self, album_id, status, message=None):
        value = {"status": status}
        if message:
            value["message"] = message
        with self._promote_status_lock:
            self._promote_status[album_id] = value

    def _run_promote(self, album_id):
        self._set_promote_status(album_id, "running")
        try:
            core.run_lock.acquire(blocking=True)
            try:
                status, error = self.promote_expired(album_id)
            finally:
                core.run_lock.release()
            if status is not None:
                self._set_promote_status(album_id, "failed", error)
            else:
                self._set_promote_status(album_id, "completed")
                core.log(f"Promoted expired album {album_id}.")
        except Exception as e:
            core.log(f"ERROR promoting expired album {album_id}: {e}")
            self._set_promote_status(
                album_id, "failed",
                "Could not update the playlist due to an internal error",
            )

    def promote_expired(self, album_id):
        """Add an expired album back to the main playlist. It keeps its expired
        listing (so it can be taken out again later) and is protected from
        expiry only until its retention window closes."""
        if not core.is_connected():
            return 400, "Not connected to Spotify"
        if self._expired_album(album_id) is None:
            return 404, "Unknown expired album"
        if not self.apply_override(album_id, "false"):
            return 404, "Unknown album"
        return None, None

    def return_to_expired(self, album_id):
        """Take an expired album back out of the main playlist and clear its
        promotion, leaving it listed under Expired."""
        if not core.is_connected():
            return 400, "Not connected to Spotify"
        c = core.load_config()
        album = self._expired_album(album_id, c)
        if album is None:
            return 404, "Unknown expired album"
        if not album.added_to_playlist:
            return self._clear_promotion(album_id)

        # Playlist edits must not overlap a scan: a scan holds one state
        # snapshot and saves it repeatedly for the whole run, so a concurrent
        # write here would either be clobbered or would clobber the scan.
        if not core.run_lock.acquire(blocking=False):
            return 409, "A scan is running -- try again in a moment"
        try:
            state = core.load_state()
            entry = state.known_albums.get(album_id)
            if entry is None or not entry.added_to_playlist:
                return self._clear_promotion(album_id)
            if not c.get("spotify_playlist_id"):
                return 400, "No playlist configured"
            try:
                token = core.get_access_token(
                    c["spotify_client_id"], c["spotify_client_secret"],
                    core.load_refresh_token())
                uris = entry.track_uris or core.get_album_track_uris(album_id, state)
                if uris:
                    core.remove_tracks_from_playlist(
                        token, c["spotify_playlist_id"], uris, state)
            except Exception as e:
                # Leave the entry untouched: clearing added_to_playlist while
                # the tracks are still on the playlist would make state lie.
                core.log(f"ERROR returning '{entry.name}' to expired: {e}")
                return 502, "Could not update the playlist due to an internal error"
        finally:
            core.run_lock.release()
        return self._clear_promotion(album_id)

    @staticmethod
    def _clear_promotion(album_id):
        """Atomically drop the album back to the Expired stage."""
        def _mutate(s):
            entry = s.known_albums.get(album_id)
            if entry is None:
                return None
            entry.added_to_playlist = False
            entry.track_uris = []
            entry.manual_override = None
            return s

        core.update_state(_mutate)
        return None, None

    @staticmethod
    def _expired_album(album_id, config=None):
        """The album, if it currently sits in the Expired stage."""
        c = config if config is not None else core.load_config()
        album = core.load_state().known_albums.get(album_id)
        if album is None or not core.is_expired(album, c["days_lookback"]):
            return None
        return album

"""Storage abstraction.

``Store`` is the persistence interface; ``JsonFileStore`` is the current
implementation (atomic JSON files under DATA_DIR). A future SqliteStore
could implement the same protocol without touching business logic.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from .models import State


class Store(Protocol):
    def load_state(self) -> State: ...
    def save_state(self, state: State) -> None: ...
    def update_state(self, mutator): ...
    def load_config(self) -> Optional[dict]: ...
    def save_config(self, config: dict) -> None: ...
    def load_refresh_token(self) -> Optional[str]: ...
    def save_refresh_token(self, token: str) -> None: ...


def _write_json_atomic(path: Path, payload):
    """Write JSON to a temp file in the same directory, then os.replace it
    into place (atomic on POSIX, avoids a torn write on crash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates the file mode 0600; widen it so files stay
        # readable/writable by non-root users sharing a bind mount.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class JsonFileStore:
    """JSON-file backed Store. All read-modify-write operations are
    serialized with a per-instance lock."""

    def __init__(self, state_file: Path, token_file: Path, config_file: Path):
        self.state_file = Path(state_file)
        self.token_file = Path(token_file)
        self.config_file = Path(config_file)
        self._lock = threading.RLock()

    # --- state -------------------------------------------------------------

    def load_state(self) -> State:
        with self._lock:
            if self.state_file.exists():
                with open(self.state_file) as f:
                    return State.from_dict(json.load(f))
        return State()

    def save_state(self, state: State) -> None:
        with self._lock:
            _write_json_atomic(self.state_file, state.to_dict())

    def update_state(self, mutator):
        """Atomically load -> mutate -> save. ``mutator`` receives the loaded
        ``State``; returning None leaves the file untouched. Returns the
        (possibly mutated) State."""
        with self._lock:
            state = self.load_state()
            result = mutator(state)
            if result is not None:
                state = result
                self.save_state(state)
            return state

    # --- config ------------------------------------------------------------

    def load_config(self) -> Optional[dict]:
        with self._lock:
            if self.config_file.exists():
                with open(self.config_file) as f:
                    return json.load(f)
        return None

    def save_config(self, config: dict) -> None:
        with self._lock:
            _write_json_atomic(self.config_file, config)

    # --- OAuth refresh token -------------------------------------------------

    def save_refresh_token(self, token: str) -> None:
        with self._lock:
            _write_json_atomic(self.token_file, {
                "refresh_token": token,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            })
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def load_refresh_token(self) -> Optional[str]:
        with self._lock:
            if self.token_file.exists():
                with open(self.token_file) as f:
                    return json.load(f).get("refresh_token")
        # allow seeding via env on first boot
        return os.environ.get("SPOTIFY_REFRESH_TOKEN")

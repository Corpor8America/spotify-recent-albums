"""Logging for spotify_core.

A stdlib ``logging`` logger ("spotify_core") feeds two handlers:
a console StreamHandler (stdout, so docker logs keep working) and a
RingBufferHandler that keeps the last 500 formatted lines in memory for
the dashboard's "Recent activity" panel.

Structured fields can be attached via ``log(message, artist_id=..., ...)``
and are rendered as a JSON suffix in the ring buffer.
"""

import json
import logging
import sys
import threading
from datetime import datetime

_logger = logging.getLogger("spotify_core")

_ring_buffer = []
_ring_lock = threading.Lock()
_configure_lock = threading.Lock()
_configured = False


class RingBufferHandler(logging.Handler):
    """Keeps the most recent log lines in memory for the dashboard."""

    def emit(self, record):
        message = record.getMessage()
        fields = getattr(record, "fields", None)
        if fields:
            try:
                message = f"{message} {json.dumps(fields, ensure_ascii=False, sort_keys=True)}"
            except (TypeError, ValueError):
                pass
        line = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        with _ring_lock:
            _ring_buffer.append(line)
            del _ring_buffer[:-500]


def configure_logging():
    """Idempotently attach the ring buffer and console handlers."""
    global _configured
    with _configure_lock:
        if _configured:
            return
        _logger.addHandler(RingBufferHandler())
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(console)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False
        _configured = True


def log(message, **fields):
    """Log an informational message; ``fields`` become structured context."""
    configure_logging()
    _logger.info(message, extra={"fields": fields})


def get_recent_logs():
    with _ring_lock:
        return list(_ring_buffer)


def clear_logs():
    with _ring_lock:
        _ring_buffer.clear()

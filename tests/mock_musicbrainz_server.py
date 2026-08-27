"""
Mock MusicBrainz API server.

Simulates the MusicBrainz Web API surface used by this project:
  - GET /ws/2/url  (Spotify-to-MBID resolution)
  - GET /ws/2/artist/{mbid}  (artist lookup with optional release-groups)

Also exposes /_control/* endpoints so the test harness can configure
the simulated catalog and inspect request counts.

Run standalone for manual poking:
    python mock_musicbrainz_server.py --port 8792

Or import `MockMusicBrainzServer` and use it programmatically:

    server = MockMusicBrainzServer()
    server.start()
    ...
    server.stop()
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def _default_artist_catalog():
    """Return a default catalog with a few test artists."""
    return {
        "mb-artist-001": {
            "name": "Active Band",
            "life_span": {"ended": False},
            "release-groups": [
                {"id": "rg-001", "title": "Recent Album", "primary-type": "Album",
                 "first-release-date": "2025-06-01"},
                {"id": "rg-002", "title": "Upcoming Album", "primary-type": "Album",
                 "first-release-date": "2099-01-15"},
                {"id": "rg-003", "title": "Old Album", "primary-type": "Album",
                 "first-release-date": "2020-03-10"},
                {"id": "rg-004", "title": "Single Release", "primary-type": "Single",
                 "first-release-date": "2025-08-01"},
            ],
        },
        "mb-artist-002": {
            "name": "Inactive Band",
            "life_span": {"ended": True, "begin": "2000-01-01", "end": "2020-12-31"},
            "release-groups": [
                {"id": "rg-101", "title": "Final Album", "primary-type": "Album",
                 "first-release-date": "2020-01-01"},
            ],
        },
        "mb-artist-003": {
            "name": "Future Releases Band",
            "life_span": {"ended": False},
            "release-groups": [
                {"id": "rg-201", "title": "Next Album", "primary-type": "Album",
                 "first-release-date": "2099-06-01"},
                {"id": "rg-202", "title": "Also Upcoming", "primary-type": "Album",
                 "first-release-date": "2099-12-25"},
            ],
        },
    }


def _default_artist_mappings():
    """Return default Spotify artist ID -> MBID mappings."""
    return {
        "spotify-artist-001": "mb-artist-001",
        "spotify-artist-002": "mb-artist-002",
        "spotify-artist-003": "mb-artist-003",
    }


class _State:
    """Shared, thread-safe mutable state for the mock server."""

    def __init__(self):
        self.lock = threading.Lock()
        self.artist_catalog = _default_artist_catalog()
        self.artist_mappings = _default_artist_mappings()
        self.rate_limit_503_every = None  # inject 503 every N requests
        self.total_requests = 0
        self.request_log = []  # list of (method, path)

    def snapshot(self):
        with self.lock:
            return {
                "total_requests": self.total_requests,
                "request_log": list(self.request_log),
            }


class _Handler(BaseHTTPRequestHandler):
    state: _State = None  # set by MockMusicBrainzServer

    def log_message(self, format, *args):
        pass  # keep test output quiet

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_503(self):
        """Returns True if this request should get a 503 (for retry testing)."""
        s = self.state
        with s.lock:
            s.total_requests += 1
            if s.rate_limit_503_every is not None and s.total_requests % s.rate_limit_503_every == 0:
                return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        with self.state.lock:
            self.state.request_log.append(("GET", path))

        if path == "/_control/snapshot":
            self._send_json(200, self.state.snapshot())
            return

        if self._check_503():
            self.send_response(503)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return

        if path == "/ws/2/url":
            self._handle_url_lookup(qs)
        elif path.startswith("/ws/2/artist/"):
            mbid = path.split("/")[-1]
            self._handle_artist_lookup(mbid, qs)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if path == "/_control/configure":
            cfg = json.loads(raw_body or b"{}")
            with self.state.lock:
                for key in ("artist_catalog", "artist_mappings", "rate_limit_503_every"):
                    if key in cfg:
                        setattr(self.state, key, cfg[key])
            self._send_json(200, {"ok": True})
            return

        if path == "/_control/reset":
            with self.state.lock:
                self.state.artist_catalog = _default_artist_catalog()
                self.state.artist_mappings = _default_artist_mappings()
                self.state.rate_limit_503_every = None
                self.state.total_requests = 0
                self.state.request_log = []
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "not found"})

    def _handle_url_lookup(self, qs):
        """Resolve a Spotify artist URL to a MusicBrainz MBID."""
        resource = qs.get("resource", "")
        # Extract Spotify artist ID from URL like https://open.spotify.com/artist/XXX
        parts = resource.rstrip("/").split("/")
        spotify_id = parts[-1] if parts else ""

        s = self.state
        with s.lock:
            mbid = s.artist_mappings.get(spotify_id)

        if mbid:
            self._send_json(200, {
                "relations": [
                    {
                        "type": "artist",
                        "artist": {"id": mbid, "name": f"Artist {spotify_id}"},
                    }
                ],
            })
        else:
            self._send_json(200, {"relations": []})

    def _handle_artist_lookup(self, mbid, qs):
        """Return artist data, optionally including release-groups."""
        include = qs.get("inc", "")
        s = self.state
        with s.lock:
            artist = s.artist_catalog.get(mbid)

        if not artist:
            # Return empty artist with no release-groups (matches real API behavior)
            self._send_json(200, {
                "id": mbid,
                "name": "Unknown Artist",
                "life_span": {"ended": False},
                "release-groups": [] if "release-groups" in include else [],
            })
            return

        data = {
            "id": mbid,
            "name": artist["name"],
            "life_span": artist.get("life_span", {}),
        }

        if "release-groups" in include:
            data["release-groups"] = artist.get("release-groups", [])

        self._send_json(200, data)


class MockMusicBrainzServer:
    def __init__(self, host="127.0.0.1", port=0):
        self.state = _State()
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://127.0.0.1:{port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def configure(self, **kwargs):
        with self.state.lock:
            for k, v in kwargs.items():
                setattr(self.state, k, v)

    def snapshot(self):
        return self.state.snapshot()

    def reset(self):
        with self.state.lock:
            self.state.artist_catalog = _default_artist_catalog()
            self.state.artist_mappings = _default_artist_mappings()
            self.state.rate_limit_503_every = None
            self.state.total_requests = 0
            self.state.request_log = []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the mock MusicBrainz server standalone")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    server = MockMusicBrainzServer(host=args.host, port=args.port)
    server.start()
    print(f"Mock MusicBrainz server running at {server.base_url}")
    print("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()

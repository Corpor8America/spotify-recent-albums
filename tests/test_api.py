import time
import unittest
from unittest.mock import patch

import spotify_core as core
from spotify_core.api import RateLimiter, blocked_until, endpoint_category
from spotify_core.models import State
from tests.support import ContextTestCase


class EndpointCategoryTests(ContextTestCase):
    def test_normalizes_ids(self):
        url = f"{self.ctx.spotify_api_base}/artists/abc123def456ghi/albums"
        cat = core.endpoint_category("GET", url)
        self.assertIn("{id}", cat)
        self.assertNotIn("abc123def456ghi", cat)

    def test_strips_api_base(self):
        url = f"{self.ctx.spotify_api_base}/me/following"
        cat = core.endpoint_category("GET", url)
        self.assertEqual(cat, "GET /me/following")


class RateLimiterTests(unittest.TestCase):
    def test_no_delay_when_under_limit(self):
        limiter = RateLimiter(120, min_interval_seconds=0)
        import time as _time
        start = _time.monotonic()
        limiter.wait_if_needed()
        elapsed = _time.monotonic() - start
        self.assertLess(elapsed, 1)

    def test_respects_min_interval(self):
        limiter = RateLimiter(120, min_interval_seconds=2)
        limiter.last_request_time = time.time() - 0.5
        import time as _time
        start = _time.monotonic()
        limiter.wait_if_needed()
        elapsed = _time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.0)


class BlockedUntilTests(unittest.TestCase):
    def test_returns_future_only(self):
        future = int(time.time()) + 3600
        past = int(time.time()) - 3600
        state = State(rate_limits={"GET /artists/{id}/albums": future})
        self.assertEqual(blocked_until(state, "GET /artists/{id}/albums"), future)
        state = State(rate_limits={"GET /artists/{id}/albums": past})
        self.assertIsNone(blocked_until(state, "GET /artists/{id}/albums"))
        self.assertIsNone(blocked_until(State(), "GET /artists/{id}/albums"))


if __name__ == "__main__":
    unittest.main()

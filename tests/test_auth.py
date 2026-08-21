import unittest

import spotify_core as core
from tests.support import ContextTestCase


class GetAuthUrlTests(ContextTestCase):
    def test_url_contains_required_params(self):
        url = core.get_auth_url("my-client-id", "http://localhost:8080/callback", "csrf123")
        self.assertIn("client_id=my-client-id", url)
        self.assertIn("redirect_uri=http", url)
        self.assertIn("response_type=code", url)
        self.assertIn("state=csrf123", url)
        self.assertIn("scope=", url)
        self.assertIn("user-follow-read", url)
        self.assertIn("playlist-modify", url)

    def test_url_starts_with_auth_endpoint(self):
        url = core.get_auth_url("cid", "http://x/callback", "s")
        self.assertTrue(url.startswith(core.get_context().spotify_auth_url))


class TokenFileTests(ContextTestCase):
    def test_save_and_load(self):
        core.save_refresh_token("test_token_123")
        loaded = core.load_refresh_token()
        self.assertEqual(loaded, "test_token_123")

    def test_load_returns_none_when_missing(self):
        import os
        os.environ.pop("SPOTIFY_REFRESH_TOKEN", None)
        self.assertIsNone(core.load_refresh_token())

    def test_is_connected_false_when_no_token(self):
        import os
        os.environ.pop("SPOTIFY_REFRESH_TOKEN", None)
        self.assertFalse(core.is_connected())

    def test_is_connected_true_with_token(self):
        core.save_refresh_token("token")
        self.assertTrue(core.is_connected())


if __name__ == "__main__":
    unittest.main()

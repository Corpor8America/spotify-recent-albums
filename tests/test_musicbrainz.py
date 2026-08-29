"""Unit tests for spotify_core.musicbrainz API functions.

All tests mock requests.get and _rate_limit to avoid real HTTP calls and sleeps.
"""

import unittest
from unittest.mock import MagicMock, patch

from spotify_core.musicbrainz import (
    get_artist_active,
    get_artist_release_groups,
    get_albums_in_window,
    get_albums_with_future_dates,
    mb_request,
    resolve_spotify_to_mb,
)


def _mock_response(status_code=200, json_data=None, headers=None):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from requests.exceptions import HTTPError
        err = HTTPError(response=resp)
        resp.raise_for_status.side_effect = err
    return resp


class MbRequestTests(unittest.TestCase):

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_json_on_200(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {"key": "value"})
        result = mb_request("https://example.com/api")
        self.assertEqual(result, {"key": "value"})

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_sets_user_agent_header(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {})
        mb_request("https://example.com/api")
        call_kwargs = mock_get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        self.assertIn("User-Agent", headers)
        self.assertIn("SpotifyRecentlyReleasedAlbums", headers["User-Agent"])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.time.sleep")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_retries_on_503_then_succeeds(self, mock_get, mock_sleep, mock_rl):
        mock_get.side_effect = [
            _mock_response(503, headers={"Retry-After": "0"}),
            _mock_response(200, {"ok": True}),
        ]
        result = mb_request("https://example.com/api")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_get.call_count, 2)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.time.sleep")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_waits_for_backoff_when_retry_after_is_zero(self, mock_get, mock_sleep, mock_rl):
        mock_get.return_value = _mock_response(503, headers={"Retry-After": "0"})
        result = mb_request("https://example.com/api")
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)
        for call in mock_sleep.call_args_list:
            self.assertGreaterEqual(call[0][0], 1.0)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.time.sleep")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_respects_retry_after(self, mock_get, mock_sleep, mock_rl):
        mock_get.side_effect = [
            _mock_response(503, headers={"Retry-After": "7"}),
            _mock_response(200, {"ok": True}),
        ]
        result = mb_request("https://example.com/api")
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(mock_sleep.call_args_list[0][0][0], 7.0)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.time.sleep")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_exhausts_retries_on_persistent_503(self, mock_get, mock_sleep, mock_rl):
        mock_get.return_value = _mock_response(503, headers={"Retry-After": "0"})
        result = mb_request("https://example.com/api")
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_raises_on_non_503_http_error(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(404)
        with self.assertRaises(Exception):
            mb_request("https://example.com/api")

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_passes_params(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {})
        mb_request("https://example.com/api", params={"fmt": "json"})
        call_kwargs = mock_get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        self.assertEqual(params, {"fmt": "json"})


class ResolveSpotifyToMbTests(unittest.TestCase):

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_extracts_mbid_from_relations(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "relations": [
                {"type": "free streaming", "target-type": "artist", "artist": {"id": "mb-123", "name": "Test"}},
            ],
        })
        result = resolve_spotify_to_mb("spotify-123")
        self.assertEqual(result, "mb-123")

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_none_when_no_artist_relation(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "relations": [{"type": "label", "label": {"id": "x"}}],
        })
        result = resolve_spotify_to_mb("spotify-123")
        self.assertIsNone(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_none_when_relation_has_no_id(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "relations": [{"type": "free streaming", "target-type": "artist", "artist": {"name": "No ID"}}],
        })
        result = resolve_spotify_to_mb("spotify-123")
        self.assertIsNone(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_none_when_api_returns_none(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, None)
        result = resolve_spotify_to_mb("spotify-123")
        self.assertIsNone(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_none_on_exception(self, mock_get, mock_rl):
        mock_get.side_effect = Exception("network error")
        result = resolve_spotify_to_mb("spotify-123")
        self.assertIsNone(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_builds_correct_url_and_params(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {"relations": []})
        resolve_spotify_to_mb("abc123")
        call_args = mock_get.call_args
        call_url = call_args[0][0]
        call_params = call_args[1].get("params", call_args.kwargs.get("params", {}))
        self.assertIn("/ws/2/url", call_url)
        self.assertEqual(call_params["resource"], "https://open.spotify.com/artist/abc123")
        self.assertEqual(call_params["inc"], "artist-rels")
        self.assertEqual(call_params["fmt"], "json")


class GetArtistReleaseGroupsTests(unittest.TestCase):

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_filters_only_albums(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Album", "primary-type": "Album",
                 "first-release-date": "2025-01-01"},
                {"id": "rg2", "title": "Single", "primary-type": "Single",
                 "first-release-date": "2025-02-01"},
                {"id": "rg3", "title": "EP", "primary-type": "EP",
                 "first-release-date": "2025-03-01"},
            ],
        })
        result = get_artist_release_groups(None, "mb-123")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "rg1")

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_empty_on_none_response(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, None)
        result = get_artist_release_groups(None, "mb-123")
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_empty_on_empty_release_groups(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {"release-groups": []})
        result = get_artist_release_groups(None, "mb-123")
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_paginates_with_offset(self, mock_get, mock_rl):
        mock_get.side_effect = [
            _mock_response(200, {"release-groups": [
                {"id": f"rg{i}", "title": f"A{i}", "primary-type": "Album",
                 "first-release-date": "2020-01-01"} for i in range(100)]}),
            _mock_response(200, {"release-groups": [
                {"id": "rg-extra", "title": "Extra", "primary-type": "Album",
                 "first-release-date": "2020-01-01"}]}),
        ]
        result = get_artist_release_groups(None, "mb-123")
        self.assertEqual(len(result), 101)
        offsets = [
            call.kwargs["params"]["offset"]
            for call in mock_get.call_args_list
        ]
        self.assertEqual(offsets, [0, 100])


class GetArtistActiveTests(unittest.TestCase):

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_true_when_not_ended(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "life_span": {"ended": False},
        })
        result = get_artist_active("mb-123")
        self.assertTrue(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_false_when_ended(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "life_span": {"ended": True, "begin": "2000", "end": "2020"},
        })
        result = get_artist_active("mb-123")
        self.assertFalse(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_true_on_none_response(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, None)
        result = get_artist_active("mb-123")
        self.assertTrue(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_true_on_exception(self, mock_get, mock_rl):
        mock_get.side_effect = Exception("network error")
        result = get_artist_active("mb-123")
        self.assertTrue(result)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_defaults_true_when_no_life_span(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {"name": "Artist"})
        result = get_artist_active("mb-123")
        self.assertTrue(result)


class GetAlbumsWithFutureDatesTests(unittest.TestCase):

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_only_future_dates(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Old", "primary-type": "Album",
                 "first-release-date": "2020-01-01"},
                {"id": "rg2", "title": "Future", "primary-type": "Album",
                 "first-release-date": "2099-12-31"},
            ],
        })
        result = get_albums_with_future_dates(None, "mb-123")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "rg2")

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_empty_when_no_future(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Old", "primary-type": "Album",
                 "first-release-date": "2020-01-01"},
            ],
        })
        result = get_albums_with_future_dates(None, "mb-123")
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_skips_empty_release_date(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Unknown", "primary-type": "Album",
                 "first-release-date": ""},
            ],
        })
        result = get_albums_with_future_dates(None, "mb-123")
        self.assertEqual(result, [])


class GetAlbumsInWindowTests(unittest.TestCase):

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_only_dates_within_window(self, mock_get, mock_rl):
        from datetime import datetime, timedelta
        today = datetime.now()
        recent = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        old = (today - timedelta(days=400)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "In Window", "primary-type": "Album",
                 "first-release-date": recent},
                {"id": "rg2", "title": "Too Old", "primary-type": "Album",
                 "first-release-date": old},
                {"id": "rg3", "title": "Future", "primary-type": "Album",
                 "first-release-date": future},
            ],
        })
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "rg1")

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_excludes_future_dates(self, mock_get, mock_rl):
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Future", "primary-type": "Album",
                 "first-release-date": future},
            ],
        })
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_excludes_dates_older_than_window(self, mock_get, mock_rl):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Old", "primary-type": "Album",
                 "first-release-date": old},
            ],
        })
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_handles_year_precision_dates(self, mock_get, mock_rl):
        from datetime import datetime, timedelta
        this_year = str(datetime.now().year)
        last_year = str(datetime.now().year - 1)
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "This Year", "primary-type": "Album",
                 "first-release-date": this_year},
                {"id": "rg2", "title": "Last Year", "primary-type": "Album",
                 "first-release-date": last_year},
            ],
        })
        result = get_albums_in_window(None, "mb-123", 365)
        rg_ids = [r["id"] for r in result]
        self.assertIn("rg1", rg_ids)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_handles_year_month_precision_dates(self, mock_get, mock_rl):
        from datetime import datetime, timedelta
        now = datetime.now()
        this_month = now.strftime("%Y-%m")
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "This Month", "primary-type": "Album",
                 "first-release-date": this_month},
            ],
        })
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(len(result), 1)

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_empty_on_none_response(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, None)
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_returns_empty_on_empty_release_groups(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {"release-groups": []})
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(result, [])

    @patch("spotify_core.musicbrainz._rate_limit")
    @patch("spotify_core.musicbrainz.requests.get")
    def test_skips_empty_release_date(self, mock_get, mock_rl):
        mock_get.return_value = _mock_response(200, {
            "release-groups": [
                {"id": "rg1", "title": "Unknown", "primary-type": "Album",
                 "first-release-date": ""},
            ],
        })
        result = get_albums_in_window(None, "mb-123", 365)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

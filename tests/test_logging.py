import unittest

from spotify_core.logging import clear_logs, get_recent_logs, log


class LogTests(unittest.TestCase):
    def setUp(self):
        clear_logs()

    def tearDown(self):
        clear_logs()

    def test_log_appends_line(self):
        log("test message")
        logs = get_recent_logs()
        self.assertTrue(any("test message" in line for line in logs))

    def test_log_with_fields(self):
        log("fetching albums", artist_id="abc")
        logs = get_recent_logs()
        self.assertTrue(any('"artist_id": "abc"' in line for line in logs))

    def test_clear_logs_empties(self):
        log("hello")
        clear_logs()
        self.assertEqual(len(get_recent_logs()), 0)


if __name__ == "__main__":
    unittest.main()

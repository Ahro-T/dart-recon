import unittest
from unittest import mock

from dart_recon import client


class Breaker(unittest.TestCase):
    def setUp(self):
        client.reset_breaker()
        self.addCleanup(client.reset_breaker)
        p = mock.patch.object(client, "api_key", return_value="k")
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(client.time, "sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)

    def test_three_consecutive_resets_trip_and_stop_requests(self):
        with mock.patch.object(client, "_fetch", side_effect=ConnectionResetError) as f:
            for _ in range(2):
                with self.assertRaises(ConnectionResetError):
                    client._get("x.json", {})
            with self.assertRaises(client.BlockedError):
                client._get("x.json", {})
            with self.assertRaises(client.BlockedError):  # 이후에는 요청 자체를 보내지 않는다
                client._get("x.json", {})
            self.assertEqual(f.call_count, 3)
        self.assertTrue(client.tripped())

    def test_empty_response_counts_and_success_resets(self):
        with mock.patch.object(client, "_fetch", side_effect=[b"", b"", b"{}", b"", b""]):
            for expected in (ConnectionError, ConnectionError):
                with self.assertRaises(expected):
                    client._get("x.json", {})
            self.assertEqual(client._get("x.json", {}), b"{}")
            for _ in range(2):
                with self.assertRaises(ConnectionError):
                    client._get("x.json", {})
        self.assertFalse(client.tripped())

    def test_blocked_error_is_not_swallowed_as_runtime_error(self):
        self.assertFalse(issubclass(client.BlockedError, (RuntimeError, OSError)))

    def test_financials_does_not_retry(self):
        with mock.patch.object(client, "_fetch", side_effect=ConnectionResetError) as f, \
             mock.patch.dict(client.os.environ, {"DART_OFFLINE": "0"}), \
             mock.patch.object(client, "CACHE", client.Path("/nonexistent-cache")):
            with self.assertRaises(ConnectionResetError):
                client.financials("00000000", 2025)
            self.assertEqual(f.call_count, 1)

    def test_min_interval_is_at_least_one_second_and_enforced(self):
        self.assertGreaterEqual(client.MIN_INTERVAL, 1.0)
        with mock.patch.object(client, "_fetch", return_value=b"{}"):
            client._get("x.json", {})
            client._get("x.json", {})
        waited = [c.args[0] for c in self.sleep.call_args_list]
        self.assertTrue(waited and 0.5 < waited[-1] <= client.MIN_INTERVAL)


if __name__ == "__main__":
    unittest.main()

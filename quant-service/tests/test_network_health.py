from __future__ import annotations

import unittest

from app.network_health import NetworkStateTracker


class NetworkStateTrackerTests(unittest.TestCase):
    def test_transient_failures_require_independent_sources_before_offline_and_recover(self) -> None:
        tracker = NetworkStateTracker(failure_threshold=2)
        tracker.record_failure("tushare:super", "ConnectTimeout token=secret")
        self.assertEqual(tracker.snapshot()["state"], "degraded")
        tracker.record_failure("tushare:super", "proxy unavailable")
        self.assertEqual(tracker.snapshot()["state"], "degraded")
        self.assertEqual(tracker.snapshot()["consecutive_failure_sources"], ["tushare:super"])
        tracker.record_failure("public:tencent", "ConnectTimeout")
        self.assertEqual(tracker.snapshot()["state"], "offline")
        tracker.record_success("tushare:super")
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["state"], "recovering")
        self.assertEqual(snapshot["last_error"], None)
        self.assertEqual(snapshot["recovery_count"], 1)
        self.assertEqual(snapshot["consecutive_failure_sources"], [])
        tracker.record_success("tushare:super")
        self.assertEqual(tracker.snapshot()["state"], "online")

    def test_permission_failure_does_not_mark_machine_offline(self) -> None:
        tracker = NetworkStateTracker(failure_threshold=1)
        tracker.record_failure("tushare:super", "HTTP 403 permission denied", transient=False)
        self.assertEqual(tracker.snapshot()["state"], "unknown")


if __name__ == "__main__":
    unittest.main()

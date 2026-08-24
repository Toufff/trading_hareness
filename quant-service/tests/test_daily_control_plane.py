import unittest
from datetime import date

from app.daily_control_plane import EQUITY_DAILY_CONTROL_STATUS_SQL, status_payload


class DailyControlPlaneTests(unittest.TestCase):
    def test_index_rows_do_not_participate_in_equity_control_gate(self):
        self.assertIn("universe_key='all_a'", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("expected_daily_rows", EQUITY_DAILY_CONTROL_STATUS_SQL)
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "expected_daily_rows": 5_000,
            "daily_rows": 4_950, "adjustment_rows": 4_950, "limit_rows": 4_950,
        })
        self.assertEqual(payload["state"], "ready")
        self.assertIsNone(payload["reason"])
        self.assertEqual(payload["minimum_required_rows"], 4_750)

    def test_missing_equity_controls_remain_fail_closed(self):
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "daily_rows": 3447,
            "adjustment_rows": 3446, "limit_rows": 3447,
        })
        self.assertEqual(payload["state"], "blocked")
        self.assertIn("missing", payload["reason"])

    def test_incomplete_daily_cross_section_remains_blocked_even_with_complete_local_controls(self):
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "expected_daily_rows": 5_549,
            "daily_rows": 3_447, "adjustment_rows": 3_447, "limit_rows": 3_447,
        })
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["coverage_ratio"], 0.6212)
        self.assertIn("point-in-time all-A", payload["reason"])

    def test_empty_result_is_absent(self):
        self.assertEqual(status_payload(None), {"state": "absent", "reason": "no canonical equity daily bars"})

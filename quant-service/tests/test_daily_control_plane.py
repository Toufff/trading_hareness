import unittest
from datetime import date

from app.daily_control_plane import EQUITY_DAILY_CONTROL_STATUS_SQL, status_payload


class DailyControlPlaneTests(unittest.TestCase):
    def test_index_rows_do_not_participate_in_equity_control_gate(self):
        self.assertIn("JOIN quant.instruments", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("instrument.list_date IS NOT NULL", EQUITY_DAILY_CONTROL_STATUS_SQL)
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "daily_rows": 3447,
            "adjustment_rows": 3447, "limit_rows": 3447,
        })
        self.assertEqual(payload["state"], "ready")
        self.assertIsNone(payload["reason"])

    def test_missing_equity_controls_remain_fail_closed(self):
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "daily_rows": 3447,
            "adjustment_rows": 3446, "limit_rows": 3447,
        })
        self.assertEqual(payload["state"], "blocked")
        self.assertIn("missing", payload["reason"])

    def test_empty_result_is_absent(self):
        self.assertEqual(status_payload(None), {"state": "absent", "reason": "no canonical equity daily bars"})

from __future__ import annotations

import unittest
from datetime import date

from app.daily_strategy_summary_service import terminal_for_exchange_date


class _Result:
    def __init__(self, row): self.row = row
    def fetchone(self): return self.row


class DailyStrategySummaryServiceTests(unittest.TestCase):
    def test_terminal_receipt_accepts_only_non_retry_delivery_states(self):
        class Connection:
            def __init__(self, row): self.row = row
            def execute(self, sql, params):
                self.sql, self.params = sql, params
                return _Result(self.row)

        complete = Connection({"?column?": 1})
        self.assertTrue(terminal_for_exchange_date(complete, date(2026, 8, 21)))
        self.assertIn("delivery_status=ANY", complete.sql)
        self.assertEqual(complete.params[1], ["sent", "disabled", "suppressed"])
        self.assertFalse(terminal_for_exchange_date(Connection(None), date(2026, 8, 21)))

from __future__ import annotations

import unittest

from app.technical_analysis import rsi, technical_summary


class RsiTests(unittest.TestCase):
    def test_needs_period_plus_one_closes(self):
        # A true 14-period RSI needs 15 closes (14 changes plus the anchor);
        # exactly 14 closes must not produce a value.
        self.assertIsNone(rsi([float(value) for value in range(14)], period=14))
        self.assertIsNotNone(rsi([float(value) for value in range(15)], period=14))

    def test_uses_exactly_the_last_period_plus_one_closes(self):
        # A strictly increasing series should read as maximally overbought
        # (100) regardless of how much extra history precedes the window.
        rising = [float(value) for value in range(1, 40)]
        self.assertEqual(rsi(rising, period=14), 100.0)

    def test_flat_series_is_100_by_this_codebase_convention(self):
        flat = [10.0] * 20
        self.assertEqual(rsi(flat, period=14), 100.0)

    def test_technical_summary_still_reports_rsi_14(self):
        rows = [{"trade_date": f"2026-08-{day:02d}", "close": 10.0 + day * 0.1} for day in range(1, 25)]
        result = technical_summary(rows)
        self.assertIn("rsi_14", result)
        self.assertIsNotNone(result["rsi_14"])


if __name__ == "__main__":
    unittest.main()

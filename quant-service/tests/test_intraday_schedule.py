"""Capacity and cadence guards shared by realtime monitor paths."""

from __future__ import annotations

import unittest

from app.intraday_schedule import INTRADAY_WATCHLIST_MAX_SYMBOLS, intraday_watchlist_capacity


class IntradayScheduleCapacityTests(unittest.TestCase):
    def test_watchlist_overflow_blocks_instead_of_silently_truncating(self) -> None:
        within = intraday_watchlist_capacity(INTRADAY_WATCHLIST_MAX_SYMBOLS)
        overflow = intraday_watchlist_capacity(INTRADAY_WATCHLIST_MAX_SYMBOLS + 1)

        self.assertFalse(within["blocked"])
        self.assertTrue(overflow["blocked"])
        self.assertEqual(overflow["max_symbols"], INTRADAY_WATCHLIST_MAX_SYMBOLS)
        self.assertIn("exceeds", str(overflow["reason"]))


if __name__ == "__main__":
    unittest.main()

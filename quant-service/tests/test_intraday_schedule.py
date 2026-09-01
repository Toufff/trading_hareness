"""Capacity and cadence guards shared by realtime monitor paths."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.intraday_schedule import INTRADAY_WATCHLIST_MAX_SYMBOLS, intraday_watchlist_capacity, intraday_watchlist_max_symbols


class IntradayScheduleCapacityTests(unittest.TestCase):
    def test_watchlist_overflow_blocks_instead_of_silently_truncating(self) -> None:
        within = intraday_watchlist_capacity(INTRADAY_WATCHLIST_MAX_SYMBOLS)
        overflow = intraday_watchlist_capacity(INTRADAY_WATCHLIST_MAX_SYMBOLS + 1)

        self.assertFalse(within["blocked"])
        self.assertTrue(overflow["blocked"])
        self.assertEqual(overflow["max_symbols"], INTRADAY_WATCHLIST_MAX_SYMBOLS)
        self.assertIn("exceeds", str(overflow["reason"]))

    def test_expanded_pool_is_explicitly_configurable_but_bounded(self) -> None:
        with patch.dict("os.environ", {"INTRADAY_WATCHLIST_MAX_SYMBOLS": "100"}):
            self.assertEqual(intraday_watchlist_max_symbols(), 100)
            self.assertFalse(intraday_watchlist_capacity(100)["blocked"])
            self.assertTrue(intraday_watchlist_capacity(101)["blocked"])
        with patch.dict("os.environ", {"INTRADAY_WATCHLIST_MAX_SYMBOLS": "1000"}):
            self.assertEqual(intraday_watchlist_max_symbols(), 100)


if __name__ == "__main__":
    unittest.main()

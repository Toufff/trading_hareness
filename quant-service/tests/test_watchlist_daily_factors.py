from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from app.watchlist_daily_factors import (
    daily_factors_from_rows,
    watchlist_daily_factors,
    watchlist_daily_factors_by_symbol,
)


def _number(value: object) -> float | None:
    return float(value) if value is not None else None


def _bars(days: int = 25, start: float = 10.0) -> list[dict[str, object]]:
    return [
        {
            "trading_date": date(2026, 7, day), "high": start + day / 10 + 0.2, "low": start + day / 10 - 0.2,
            "close": start + day / 10, "volume": 1000 + day, "adj_factor": 1.0,
            "is_suspended": False, "limit_up": (start + day / 10) * 1.1, "limit_down": (start + day / 10) * 0.9,
            "is_st": False,
        }
        for day in range(1, days + 1)
    ]


class DailyFactorsFromRowsCurrentLimitTests(unittest.TestCase):
    def test_current_limit_overrides_stale_last_bar_limit(self) -> None:
        # The last historical bar's own limit_up describes yesterday, not
        # today; when the caller supplies today's real published band it
        # must win.
        result = daily_factors_from_rows(_bars(), number=_number, current_limit_up=99.0, current_limit_down=88.0)
        self.assertEqual(result["trade_constraints"]["limit_up"], 99.0)
        self.assertEqual(result["trade_constraints"]["limit_down"], 88.0)

    def test_falls_back_to_the_last_bar_when_no_current_limit_supplied(self) -> None:
        bars = _bars()
        result = daily_factors_from_rows(bars, number=_number)
        self.assertEqual(result["trade_constraints"]["limit_up"], bars[-1]["limit_up"])

    def test_volume_ratio_baseline_excludes_todays_own_volume(self) -> None:
        bars = _bars()
        result = daily_factors_from_rows(bars, number=_number)
        expected_baseline = sum(bar["volume"] for bar in bars[-21:-1]) / 20
        self.assertAlmostEqual(result["volume_ratio20"], bars[-1]["volume"] / expected_baseline, places=3)


class WatchlistDailyFactorsSingleQueryTests(unittest.TestCase):
    def test_uses_exactly_one_query_for_bars_and_todays_limit(self) -> None:
        connection = MagicMock()
        rows = [
            {**bar, "current_limit_up": 99.0, "current_limit_down": 88.0}
            for bar in _bars()
        ]
        connection.execute.return_value.fetchall.return_value = rows
        result = watchlist_daily_factors(
            "000001.SZ", connection, number=_number,
            observed_at=datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trade_constraints"]["limit_up"], 99.0)

    def test_query_bounds_trading_date_and_available_at(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        watchlist_daily_factors(
            "000001.SZ", connection, number=_number,
            observed_at=datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc),
        )
        sql, params = connection.execute.call_args.args
        self.assertIn("b.trading_date<%s", sql)
        self.assertIn("b.available_at<=%s", sql)
        self.assertIn("quant.daily_trade_limits", sql)
        self.assertEqual(params[0], "000001.SZ")
        self.assertEqual(params[1], date(2026, 9, 1))


class WatchlistDailyFactorsBySymbolSingleQueryTests(unittest.TestCase):
    def test_uses_exactly_one_query_for_the_whole_basket(self) -> None:
        connection = MagicMock()
        rows = []
        for symbol in ("000001.SZ", "600000.SH"):
            for bar in _bars():
                rows.append({"symbol": symbol, **bar, "current_limit_up": 99.0, "current_limit_down": 88.0})
        connection.execute.return_value.fetchall.return_value = rows
        factors = watchlist_daily_factors_by_symbol(
            ["000001.SZ", "600000.SH"], connection, number=_number,
            observed_at=datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(factors["000001.SZ"]["trade_constraints"]["limit_up"], 99.0)
        self.assertEqual(factors["600000.SH"]["trade_constraints"]["limit_up"], 99.0)

    def test_query_bounds_trading_date_and_available_at(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        watchlist_daily_factors_by_symbol(
            ["000001.SZ"], connection, number=_number,
            observed_at=datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc),
        )
        sql, params = connection.execute.call_args.args
        self.assertIn("b.trading_date<%s", sql)
        self.assertIn("b.available_at<=%s", sql)
        self.assertIn("quant.daily_trade_limits", sql)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import date
from unittest.mock import patch

from app.factor_lab import (
    MAX_LEGACY_UNIVERSE_SYMBOLS, LegacyFactorEngineLimitError,
    factor_at, load_universe_bars, max_drawdown, pearson, rank,
    run_multi_factor_strategy,
)


class FactorLabTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            {"close": 10 + index * 0.2, "high": 10.3 + index * 0.2, "low": 9.8 + index * 0.2,
             "volume": 100 + index, "adj_factor": 1.0}
            for index in range(25)
        ]

    def test_rank_and_rank_ic_are_deterministic(self):
        self.assertEqual(rank([3.0, 1.0, 2.0]), [3.0, 1.0, 2.0])
        self.assertAlmostEqual(pearson(rank([1.0, 2.0, 3.0]), rank([4.0, 5.0, 6.0])) or 0, 1.0)
        self.assertIsNone(pearson([1.0], [1.0]))

    def test_native_factor_windows_and_drawdown(self):
        self.assertIsNone(factor_at(self.bars, 4, "momentum_5d"))
        self.assertGreater(factor_at(self.bars, 20, "momentum_5d") or 0, 0)
        self.assertGreater(factor_at(self.bars, 20, "sma_gap_20d") or 0, 0)
        self.assertIsNotNone(factor_at(self.bars, 20, "volatility_20d"))
        self.assertAlmostEqual(max_drawdown([1.0, 1.2, 0.9, 1.1]), -0.25)

    def test_cross_session_factors_refuse_raw_price_when_adjustment_is_missing(self):
        bars = [dict(item) for item in self.bars]
        # The missing factor is inside the lookback used by the calculation,
        # not merely a later row outside the requested factor window.
        bars[20].pop("adj_factor")
        self.assertIsNone(factor_at(bars, 20, "momentum_5d"))
        self.assertIsNone(factor_at(bars, 20, "sma_gap_20d"))

    def test_legacy_loader_uses_point_in_time_membership_not_current_enabled_state(self):
        class Result:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

            def fetchall(self):
                return []

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((str(sql), params))
                return Result({"count": 1}) if "count(DISTINCT" in sql else Result()

        connection = Connection()
        self.assertEqual(load_universe_bars(connection, "all_a", date(2026, 8, 1), date(2026, 8, 14)), {})
        bars_sql, bars_params = connection.calls[-1]
        self.assertIn("quant.universe_membership_history", bars_sql)
        self.assertNotIn("quant.universe_members u", bars_sql)
        self.assertIn("membership.effective_from<=b.trading_date", bars_sql)
        self.assertIn("instrument", bars_sql)
        self.assertIn("i.delist_date IS NULL OR i.delist_date>=b.trading_date", bars_sql)
        self.assertEqual(bars_params, ("all_a", date(2026, 4, 3), date(2026, 8, 14)))

    def test_legacy_loader_fails_closed_for_broad_universe_before_loading_bars(self):
        class Result:
            def fetchone(self):
                return {"count": MAX_LEGACY_UNIVERSE_SYMBOLS + 1}

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((str(sql), params))
                return Result()

        connection = Connection()
        with self.assertRaisesRegex(LegacyFactorEngineLimitError, "bounded SQL factor engine"):
            load_universe_bars(connection, "all_a", date(2026, 8, 1), date(2026, 8, 14))
        self.assertEqual(len(connection.calls), 1)

    def test_native_strategy_holds_one_session_after_next_day_entry(self):
        signal_day = date(2026, 8, 3)
        entry_day = date(2026, 8, 4)
        exit_day = date(2026, 8, 5)

        def bars(first_close: float, second_open: float, third_close: float):
            return [
                {"trading_date": signal_day, "open": first_close, "close": first_close,
                 "pre_close": first_close, "adj_factor": 1.0, "is_suspended": False, "is_st": False},
                {"trading_date": entry_day, "open": second_open, "close": second_open,
                 "pre_close": first_close, "adj_factor": 1.0, "is_suspended": False, "is_st": False},
                {"trading_date": exit_day, "open": third_close, "close": third_close,
                 "pre_close": second_open, "adj_factor": 1.0, "is_suspended": False, "is_st": False},
            ]

        panel = {
            signal_day: {
                "000001.SZ": {"momentum_20d": 2.0},
                "000002.SZ": {"momentum_20d": 1.0},
            }
        }
        bars_by_symbol = {
            "000001.SZ": bars(10.0, 10.0, 12.0),
            "000002.SZ": bars(10.0, 10.0, 11.0),
        }
        with patch("app.factor_lab.historical_factor_panel", return_value=(panel, bars_by_symbol)):
            result = run_multi_factor_strategy(
                connection=None, universe_key="test", start_date=signal_day, end_date=signal_day,
                parameters={"factors": ["momentum_20d"], "rebalance_days": 1, "hold_days": 1, "top_n": 1},
            )

        self.assertEqual(result["metrics"]["assumptions"]["effective_exit_lag"], 2)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["entry_date"], str(entry_day))
        self.assertEqual(result["trades"][0]["exit_date"], str(exit_day))


if __name__ == "__main__":
    unittest.main()

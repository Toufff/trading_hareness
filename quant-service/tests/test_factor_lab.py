import unittest
from datetime import date

from app.factor_lab import (
    MAX_LEGACY_UNIVERSE_SYMBOLS, LegacyFactorEngineLimitError,
    factor_at, load_universe_bars, max_drawdown, pearson, rank,
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from datetime import date

from app.factor_sql_lab import (
    _bh_q_values, _materialize_evaluation_rows, _materialize_factor_scores,
    _split_rows, evaluable_factor_keys, prepare_factor_panel, run_multi_factor_strategy_sql,
)


class RecordingResult:
    def __init__(self, row=None):
        self.row = row or {"rows": 10, "symbols": 2, "days": 5}

    def fetchone(self):
        return self.row


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return RecordingResult()


class FactorSqlLabTests(unittest.TestCase):
    def test_evaluable_contract_contains_only_implemented_price_volume_factors(self):
        self.assertEqual(
            evaluable_factor_keys(),
            frozenset({
                "momentum_5d", "momentum_20d", "reversal_5d", "sma_gap_20d",
                "volatility_20d", "volume_ratio_20d", "intraday_strength",
            }),
        )

    def test_chronological_split_purges_both_boundaries(self):
        rows = [{"trading_date": index} for index in range(100)]
        splits, contract = _split_rows(rows, 5)
        self.assertEqual([len(splits[key]) for key in ("train", "validation", "test")], [55, 10, 15])
        self.assertEqual(contract["dropped_boundary_days"], 20)
        self.assertLess(splits["train"][-1]["trading_date"], splits["validation"][0]["trading_date"])
        self.assertLess(splits["validation"][-1]["trading_date"], splits["test"][0]["trading_date"])

    def test_benjamini_hochberg_is_monotone_and_keeps_missing(self):
        result = _bh_q_values({"a": 0.01, "b": 0.04, "c": 0.03, "d": None})
        self.assertAlmostEqual(result["a"] or 0, 0.03)
        self.assertAlmostEqual(result["b"] or 0, 0.04)
        self.assertAlmostEqual(result["c"] or 0, 0.04)
        self.assertIsNone(result["d"])

    def test_strategy_rejects_overlapping_periods_before_database_work(self):
        with self.assertRaisesRegex(ValueError, "avoid overlapping"):
            run_multi_factor_strategy_sql(
                None, "all_a", 1, 2,
                {"factors": ["momentum_20d"], "rebalance_days": 1, "hold_days": 5},
            )

    def test_panel_uses_point_in_time_membership_and_continuous_windows(self):
        connection = RecordingConnection()
        prepare_factor_panel(connection, "all_a", date(2026, 1, 1), date(2026, 3, 1), 5)
        create_sql = next(sql for sql, _ in connection.calls if "CREATE TEMP TABLE factor_sql_panel" in sql)
        self.assertIn("quant.universe_membership_history", create_sql)
        self.assertNotIn("quant.universe_members u", create_sql)
        self.assertIn("instrument.list_date IS NULL OR instrument.list_date<=bar.trading_date", create_sql)
        self.assertIn("instrument.delist_date IS NULL OR instrument.delist_date>=bar.trading_date", create_sql)
        self.assertIn("trading_index-index_20d_ago=20", create_sql)
        self.assertIn("bar.close*bar.adj_factor", create_sql)

    def test_factor_standardization_does_not_select_on_future_outcome(self):
        connection = RecordingConnection()
        _materialize_factor_scores(connection, "momentum_20d", date(2026, 1, 1), date(2026, 3, 1))
        score_sql = next(sql for sql, _ in connection.calls if "CREATE TEMP TABLE factor_sql_factor_scores" in sql)
        self.assertNotIn("future", score_sql)
        connection.calls.clear()
        _materialize_evaluation_rows(connection, "momentum_20d", date(2026, 1, 1), date(2026, 3, 1), 5)
        evaluation_sql = next(sql for sql, _ in connection.calls if "CREATE TEMP TABLE factor_sql_evaluation" in sql)
        self.assertIn("JOIN factor_sql_panel future", evaluation_sql)


if __name__ == "__main__":
    unittest.main()

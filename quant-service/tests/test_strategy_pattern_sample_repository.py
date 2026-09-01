from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import unittest

from app.strategy_pattern_sample_repository import (
    load_strategy_pattern_sample_inputs,
    persist_strategy_pattern_run,
)


class StrategyPatternSampleRepositoryTests(unittest.TestCase):
    def test_reads_only_bounded_persisted_inputs(self) -> None:
        class Result:
            def __init__(self, *, rows=None, row=None):
                self.rows, self.row = rows or [], row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        class Database:
            def __init__(self):
                self.calls = []
                self.results = iter([
                    Result(rows=[{"row_data": {"ts_code": "000001.SZ"}, "provider_key": "super", "available_at": "a"}]),
                    Result(rows=[{"row_data": {"ts_code": "000001.SZ", "nums": 2}, "available_at": "a"}]),
                    Result(row={"prior_date": "20260814"}),
                    Result(rows=[{"row_data": {"ts_code": "000001.SZ", "limit_type": "涨停池"}}]),
                    Result(rows=[{"symbol": "000001.SZ", "trading_date": date(2026, 8, 17), "close": 10.0}]),
                ])

            @contextmanager
            def transaction(self):
                yield self

            def execute(self, sql, params=None):
                self.calls.append((str(sql), params))
                return next(self.results)

        database = Database()
        inputs = load_strategy_pattern_sample_inputs(database, date(2026, 8, 17))
        self.assertEqual(len(database.calls), 5)
        self.assertEqual(inputs.limit_rows[0]["provider_key"], "super")
        self.assertEqual(inputs.step_rows, [{"ts_code": "000001.SZ", "nums": 2}])
        self.assertEqual(inputs.prior_limit_rows, [{"ts_code": "000001.SZ", "limit_type": "涨停池"}])
        self.assertEqual(inputs.daily_rows[0]["close"], 10.0)
        self.assertEqual(database.calls[0][1], ("20260817",))
        self.assertEqual(database.calls[4][1], (["000001.SZ"], date(2026, 8, 17), date(2026, 6, 18)))

    def test_no_same_day_pool_checks_event_projection_then_skips_daily_query(self) -> None:
        class Result:
            def __init__(self, *, rows=None, row=None):
                self.rows, self.row = rows or [], row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        class Database:
            def __init__(self):
                self.calls = []
                self.results = iter([
                    Result(), Result(), Result(row=None),
                    Result(), Result(row=None),
                ])

            @contextmanager
            def transaction(self):
                yield self

            def execute(self, sql, params=None):
                self.calls.append((str(sql), params))
                return next(self.results)

        database = Database()
        inputs = load_strategy_pattern_sample_inputs(database, date(2026, 8, 17))
        self.assertEqual(inputs.limit_rows, [])
        self.assertEqual(inputs.prior_limit_rows, [])
        self.assertEqual(inputs.daily_rows, [])
        self.assertEqual(len(database.calls), 5)
        self.assertIn("quant.market_events", database.calls[3][0])

    def test_persist_replaces_one_bounded_run_without_fetching_or_reranking(self) -> None:
        class Result:
            def fetchone(self):
                return {"run_id": "run-1"}

        class Database:
            def __init__(self):
                self.calls = []

            @contextmanager
            def transaction(self):
                yield self

            def execute(self, sql, params=None):
                self.calls.append((str(sql), params))
                return Result()

        database = Database()
        run_id = persist_strategy_pattern_run(
            database, "key", date(2026, 8, 17), "completed", {"minute": "completed"}, {"selected": 1},
            [{
                "symbol": "000001.SZ", "primary_cohort": "limit_pool", "cohorts": ["limit_pool"],
                "board_context": {}, "limit_context": {}, "daily_features": {},
                "intraday_pattern": {"status": "completed"}, "risk_flags": [],
            }],
            model_version="test-v1", json_safe=lambda value: value,
        )

        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(database.calls), 3)
        self.assertIn("INSERT INTO quant.strategy_pattern_runs", database.calls[0][0])
        self.assertIn("DELETE FROM quant.strategy_pattern_samples", database.calls[1][0])
        self.assertIn("INSERT INTO quant.strategy_pattern_samples", database.calls[2][0])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import unittest

from app.ten_day_leader_rotation_repository import (
    completed_for_date,
    latest_full_market_date,
    load_ten_day_ranking_inputs,
    persist_ten_day_rotation_run,
)


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Database:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return next(self.results)


class TenDayLeaderRotationRepositoryTests(unittest.TestCase):
    def test_loads_bounded_point_in_time_daily_inputs(self) -> None:
        available = datetime(2026, 8, 13, 8, tzinfo=timezone.utc)
        database = _Database([
            _Result(row={"daily_symbols": 5001, "expected_daily_symbols": 5003, "strategy_available_at": available}),
            _Result(rows=[{"symbol": "600001.SH", "trading_date": date(2026, 8, 13), "close": 10}]),
        ])

        inputs = load_ten_day_ranking_inputs(database, date(2026, 8, 13))

        self.assertEqual(inputs.daily_symbols, 5001)
        self.assertEqual(inputs.expected_daily_symbols, 5003)
        self.assertEqual(inputs.strategy_available_at, available)
        self.assertEqual(inputs.daily_rows[0]["symbol"], "600001.SH")
        self.assertIn("universe_membership_history", database.calls[0][0])
        self.assertIn("rn<=11", database.calls[1][0])

    def test_latest_full_market_date_requires_point_in_time_coverage(self) -> None:
        database = _Database([_Result(row={"trading_date": date(2026, 8, 17)})])
        self.assertEqual(latest_full_market_date(database, 5_000), date(2026, 8, 17))
        sql, params = database.calls[0]
        self.assertIn("expected_symbols", sql)
        self.assertIn("ceil(expected.expected_symbols*0.95)", sql)
        self.assertEqual(params, (5_000,))

    def test_lane_coverage_is_settled_bars_not_adjustment_coverage(self) -> None:
        """Adjustment factors arrive on a separate, slower provider lane.

        Both coverage queries used to require ``adj_factor IS NOT NULL``, so
        the moment the identity placeholders became honest NULLs this lane
        would have reported zero covered dates and silently produced nothing
        for the whole repaired window -- which is why this ships in the same
        commit as the readiness-contract change.
        """
        database = _Database([_Result(row={"trading_date": date(2026, 9, 18)})])
        latest_full_market_date(database, 5_000)
        sql = database.calls[0][0]
        covered = sql[sql.index("covered AS ("):sql.index(") SELECT expected.trading_date")]
        self.assertNotIn("bar.adj_factor IS NOT NULL", covered)
        self.assertIn("bar.quality_status IN ('fresh','partial')", covered)
        # The CTE column counts settled bars, so it is named for that.  While
        # it was still called adjusted_symbols, the gate below read as an
        # adjustment-coverage gate it no longer is.
        self.assertIn("count(DISTINCT bar.symbol)::int AS settled_symbols", covered)
        self.assertNotIn("adjusted_symbols", sql)
        self.assertIn("WHERE covered.settled_symbols>=%s", sql)

        database = _Database([
            _Result(row={"daily_symbols": 5_101, "adjustment_covered_symbols": 0,
                         "expected_daily_symbols": 5_221, "strategy_available_at": None}),
            _Result(rows=[]),
        ])
        inputs = load_ten_day_ranking_inputs(database, date(2026, 9, 18))
        self.assertEqual(inputs.daily_symbols, 5_101)
        self.assertEqual(inputs.adjustment_covered_symbols, 0)
        coverage_sql = database.calls[0][0]
        self.assertIn("count(DISTINCT bar.symbol)::int AS daily_symbols", coverage_sql)
        self.assertIn(
            "count(DISTINCT bar.symbol) FILTER (WHERE bar.adj_factor IS NOT NULL)::int "
            "AS adjustment_covered_symbols",
            coverage_sql)
        self.assertNotIn(
            "count(DISTINCT bar.symbol) FILTER (WHERE bar.adj_factor IS NOT NULL)::int AS daily_symbols",
            coverage_sql)

    def test_persists_run_and_replaces_only_its_candidates(self) -> None:
        database = _Database([_Result(row={"run_id": "run-1"}), _Result(), _Result()])
        run_id = persist_ten_day_rotation_run(
            database, run_key="key", as_of_date=date(2026, 8, 13),
            strategy_available_at=datetime(2026, 8, 13, 8, tzinfo=timezone.utc),
            model_version="test-v1", status="completed", source_status={"daily_symbols": 5001},
            summary={"candidate_count": 1}, candidates=[{
                "symbol": "600001.SH", "name": "样本", "board": "main", "ten_day_rank": 1,
                "ten_day_return_pct": 20, "current_return_pct": 10,
                "candidate_path": "ranked_limit_continuation", "shadow_state": "cycle_context_unavailable",
                "shadow_eligible": False, "decision_eligible": False, "evidence": {},
                "reason_codes": ["strategy_available_at_missing"], "risk_flags": ["no_automatic_order"],
            }], json_safe=lambda value: value,
        )

        self.assertEqual(run_id, "run-1")
        self.assertIn("ten_day_leader_rotation_runs", database.calls[0][0])
        self.assertIn("DELETE FROM quant.ten_day_leader_rotation_candidates", database.calls[1][0])
        self.assertIn("decision_eligible", database.calls[2][0])

    def test_completion_is_exact_date_and_model_scoped(self) -> None:
        database = _Database([_Result(row={"status": "completed"})])
        self.assertTrue(completed_for_date(
            database, date(2026, 8, 13), model_version="test-v1",
        ))
        self.assertEqual(database.calls[0][1], (date(2026, 8, 13), "test-v1"))


if __name__ == "__main__":
    unittest.main()

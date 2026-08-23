from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.research_experiment_service import (
    ResearchExperimentDependencies,
    build_snapshot,
    evaluate_factors,
    research_window,
)


class _Transaction:
    def __init__(self, execute):
        self.execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _deps(database: MagicMock, **overrides):
    values = {
        "database": database,
        "china_today": lambda: date(2026, 8, 21),
        "as_utc": lambda value: value,
        "http_exception": HTTPException,
        "evaluate_factor_set": MagicMock(),
        "run_multi_factor_strategy": MagicMock(),
        "json_value": lambda value: value,
    }
    values.update(overrides)
    return ResearchExperimentDependencies(**values)


class ResearchExperimentServiceTests(unittest.TestCase):
    def test_research_window_keeps_persisted_point_in_time_bounds(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            "earliest": date(2025, 1, 2), "latest": date(2026, 8, 21),
        }

        start, end = research_window(connection, "core", None, None, http_exception=HTTPException)

        self.assertEqual(start, date(2025, 1, 2))
        self.assertEqual(end, date(2026, 8, 21))
        self.assertIn("universe_membership_history", str(connection.execute.call_args.args[0]))

    def test_factor_evaluation_rejects_unknown_factor_before_running_sql_engine(self) -> None:
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value={"earliest": date(2026, 1, 2), "latest": date(2026, 8, 21)})),
            MagicMock(fetchall=MagicMock(return_value=[{"factor_key": "momentum_20d"}])),
        ]
        database = MagicMock()
        database.transaction.return_value = _Transaction(connection.execute)
        evaluator = MagicMock()
        payload = SimpleNamespace(
            universe_key="core", start_date=None, end_date=None, factor_keys=["not_registered"], horizon_days=5,
        )

        with self.assertRaises(HTTPException) as raised:
            evaluate_factors(payload, _deps(database, evaluate_factor_set=evaluator))

        self.assertEqual(raised.exception.status_code, 422)
        evaluator.assert_not_called()

    def test_snapshot_keeps_missing_daily_controls_blocked(self) -> None:
        manifest = {
            "bars": 10, "remote_reports": 2, "benchmark_bars": 1, "equity_symbols": 2,
            "fundamental_symbols": 1, "limit_symbols": 2, "exchange_open": True, "blocking_issues": 0,
        }
        statements = []

        def execute(sql, params=()):
            statements.append((str(sql), params))
            if "SELECT (SELECT count(*)::int FROM quant.canonical_bars_daily" in str(sql):
                return MagicMock(fetchone=MagicMock(return_value=manifest))
            return MagicMock()

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)
        payload = SimpleNamespace(
            as_of_date=date(2026, 8, 21), knowledge_cutoff=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )

        result = build_snapshot(payload, _deps(database))

        self.assertEqual(result["status"], "blocked")
        inserts = [params for sql, params in statements if "INSERT INTO quant.data_snapshots" in sql]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][3], "blocked")

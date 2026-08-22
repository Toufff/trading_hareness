from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock

from app.request_models import StrategyDecisionRequest
from app.strategy_decision_service import run


class _Connection:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, statement: str, params: object = ()) -> None:
        self.sql.append(statement)


class _Database:
    def __init__(self) -> None:
        self.connection = _Connection()

    @contextmanager
    def transaction(self):
        yield self.connection


class StrategyDecisionServiceTests(unittest.TestCase):
    def _dependencies(self, database: _Database, report: dict[str, object]):
        async def run_database(operation, *args, **kwargs):
            return operation(*args)

        return {
            "db": database,
            "run_database_blocking": run_database,
            "build_intraday_report": AsyncMock(return_value=report),
            "market_regime": lambda items: ("mixed", {}),
            "select_candidates": lambda items, limit: [],
            "event_context": lambda symbols, observed_at: {},
            "tushare_lhb_context": lambda symbols, observed_at: {},
            "source_readiness": lambda observed_at: {"providers": {}},
            "tushare_realtime_validation": AsyncMock(return_value={"status": "skipped", "items": []}),
            "exchange_for": lambda symbol: symbol.rsplit(".", 1)[1],
            "json_safe": lambda value: value,
            "model_version": "test-v1",
        }

    def test_blocked_report_is_persisted_by_the_extracted_service(self) -> None:
        database = _Database()
        result = asyncio.run(run(
            StrategyDecisionRequest(session="close", validate_tushare_realtime=False),
            **self._dependencies(database, {"status": "blocked", "reason": "no snapshot"}),
        ))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("recommendation_runs" in statement for statement in database.connection.sql))

    def test_completed_empty_report_keeps_research_only_boundary(self) -> None:
        database = _Database()
        result = asyncio.run(run(
            StrategyDecisionRequest(session="close", validate_tushare_realtime=False),
            **self._dependencies(database, {"status": "completed", "items": [], "coverage": {}}),
        ))
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["decision_eligible"])
        self.assertTrue(any("recommendation_runs" in statement for statement in database.connection.sql))


if __name__ == "__main__":
    unittest.main()

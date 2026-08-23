from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import MagicMock

from app.analyst_scorecards import readiness, recompute


class _Transaction:
    def __init__(self, execute):
        self.execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class AnalystScorecardReadinessTests(unittest.TestCase):
    def test_readiness_preserves_maturity_gate_reasons(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"remote_analyst_id": "neutral", "directional_stock_claims": 0, "settled_stock_outcomes": 100},
            {"remote_analyst_id": "early", "directional_stock_claims": 4, "settled_stock_outcomes": 29},
            {"remote_analyst_id": "mature", "directional_stock_claims": 4, "settled_stock_outcomes": 30},
        ]

        result = readiness(connection)

        self.assertEqual(
            [(item["remote_analyst_id"], item["mature"], item["reason"]) for item in result],
            [
                ("neutral", True, "no_directional_stock_claims"),
                ("early", False, "fewer_than_30_settled_stock_outcomes"),
                ("mature", True, "eligible_for_scorecard_review"),
            ],
        )

    def test_recompute_uses_only_versioned_remote_claims(self) -> None:
        statements = []

        def execute(sql, params=()):
            statements.append((str(sql), params))
            return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)

        result = recompute(
            date(2026, 8, 21), cn_today=lambda: date(2026, 8, 21), db=database, readiness=lambda _connection: [],
        )

        self.assertEqual(result["scorecards"], 0)
        source_sql = statements[0][0]
        self.assertIn("quant.analyst_claims", source_sql)
        self.assertNotIn("quant.analyst_signals", source_sql)

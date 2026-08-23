from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.analyst_scorecards import readiness


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


from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app.request_models import StrategyReviewRequest
from app.strategy_review_service import build


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def execute(self, statement, params=()):
        return _Result({
            "observed_at": datetime(2026, 8, 21, 7, 30, tzinfo=timezone.utc),
            "summary": {"priced_symbols": 2},
            "payload": {"items": [{"sector_key": "demo", "net_inflow": 1}]},
            "source_status": {},
        })


class StrategyReviewServiceTests(unittest.TestCase):
    def test_projection_is_read_only_when_persistence_is_disabled(self):
        review = build(
            _Connection(),
            StrategyReviewRequest(session="close", as_of_date=date(2026, 8, 21), persist=False),
            market_state=lambda items: ("mixed", {"items": len(items)}),
            index_breadth_context=lambda *args: {"quality_flags": []},
            analyst_context=lambda *args: {"execution_eligible": False},
            json_safe=lambda value: value,
        )
        self.assertEqual(review["status"], "completed")
        self.assertEqual(review["market_state"], "mixed")
        self.assertNotIn("review_key", review)
        self.assertEqual(review["data_boundary"]["automation"], "no broker order submission")


if __name__ == "__main__":
    unittest.main()

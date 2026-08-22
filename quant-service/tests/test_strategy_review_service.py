from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app.request_models import StrategyReviewRequest
from app.strategy_review_service import build, completed_for_checkpoint


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def execute(self, statement, params=()):
        if "FROM quant.market_events" in statement or "FROM quant.canonical_bars_daily" in statement:
            return _Result(rows=[])
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

    def test_completed_checkpoint_requires_completed_persisted_report(self):
        class Connection:
            def __init__(self, row): self.row = row
            def execute(self, statement, params=()):
                self.statement, self.params = statement, params
                return _Result(self.row)

        completed = Connection({"?column?": 1})
        self.assertTrue(completed_for_checkpoint(completed, date(2026, 8, 21), "close"))
        self.assertIn("report->>'status'='completed'", completed.statement)
        self.assertEqual(completed.params, (date(2026, 8, 21), "close"))
        self.assertFalse(completed_for_checkpoint(Connection(None), date(2026, 8, 21), "close"))


if __name__ == "__main__":
    unittest.main()

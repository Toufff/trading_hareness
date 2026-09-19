"""The daily pipeline materializes the candidate ledger after recommendations.

``materialize_candidate_ledger`` copies ``quant.recommendations`` for the
session into ``strategy_daily_candidates`` under ``daily_recommendation``.
Run before ``generate_recommendations`` it only ever saw the previous run's
rows, so day D's recommendations reached the ledger only on a rerun.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock

from app.daily_pipeline import run_pipeline
from app.request_models import GenerateRequest, TushareSyncRequest


class DailyPipelineStageOrderTests(unittest.TestCase):
    def test_ledger_and_proposals_run_after_recommendations(self) -> None:
        order: list[str] = []

        def stage(name: str):
            def _operation(*_args, **_kwargs):
                return None
            _operation.__name__ = name
            return _operation

        async def tracked(operation, *_args, **_kwargs):
            order.append(operation.__name__)
            return {"status": "ready"}

        stages = {name: stage(name) for name in (
            "build_snapshot", "recompute_outcomes", "recompute_scorecards", "generate_recommendations",
            "materialize_regime", "materialize_sentiment_cycle", "materialize_candidate_ledger",
            "materialize_watchlist_proposals", "settle_xiaojie_outcomes",
        )}
        result = asyncio.run(run_pipeline(
            GenerateRequest(as_of_date=date(2026, 9, 18)),
            sync_full_market_daily=AsyncMock(return_value={"status": "completed"}),
            sync_baostock=AsyncMock(return_value={"status": "completed"}),
            sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
            tushare_request=TushareSyncRequest, full_market_request=lambda **kwargs: kwargs,
            snapshot_request=lambda as_of: {"as_of_date": as_of},
            run_database_blocking=tracked, cn_today=lambda: date(2026, 9, 18),
            **stages,
        ))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(order, [
            "build_snapshot", "materialize_regime", "materialize_sentiment_cycle", "settle_xiaojie_outcomes",
            "recompute_outcomes", "recompute_scorecards", "generate_recommendations",
            "materialize_candidate_ledger", "materialize_watchlist_proposals",
        ])


if __name__ == "__main__":
    unittest.main()

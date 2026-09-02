"""WP6: sync route handlers that touched the DB directly must now run
through the bounded database executor instead of anyio's unbounded default
threadpool (audit section B, MED: "23 个同步 def handler").

Each test patches the target router module's ``run_database_blocking`` with a
recording fake so it can assert (a) the handler is awaitable/async and
(b) the underlying work is actually submitted through the bounded call
rather than executed inline.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import patch
import unittest
from uuid import uuid4

from app.request_models import (
    AnalystMarketReviewRequest,
    AnalystPromptEvaluateRequest,
    AnalystPromptGoldLabelRequest,
    BarsImport,
    DailyBar,
    PaperAccountConfigureRequest,
    PaperDecisionAcceptRequest,
)
from app.routers.analyst_action_outcomes import build_analyst_action_outcomes_router
from app.routers.analyst_prompt_lab import build_analyst_prompt_lab_router
from app.routers.analyst_research_reads import build_analyst_research_reads_router
from app.routers.market_actions import MarketActionDependencies, build_market_actions_router
from app.routers.paper_actions import build_paper_actions_router


def _find(router, path: str):
    return next(route.endpoint for route in router.routes if route.path == path)


def _bounded_recorder(calls: list[tuple[object, tuple, dict]]):
    async def bounded(action, *args, **kwargs):
        calls.append((action, args, kwargs))
        result = action(*args)
        return result

    return bounded


class _FakeTransaction:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


class _FakeDatabase:
    def transaction(self):
        return _FakeTransaction()


class AnalystPromptLabRouterBoundaryTests(unittest.TestCase):
    def _router(self):
        return build_analyst_prompt_lab_router(
            database=_FakeDatabase(),
            materialize_fn=lambda _connection, cutoff_at: {"status": "materialized"},
            label_fn=lambda _connection, **_kwargs: {"label": "ok"},
            evaluate_fn=lambda _connection, **_kwargs: {"status": "evaluated"},
            outcome_fn=lambda _connection, cutoff_at: {"status": "recomputed"},
        )

    def test_materialize_label_evaluate_and_recompute_run_through_bounded_executor(self) -> None:
        calls: list[tuple[object, tuple, dict]] = []
        with patch("app.routers.analyst_prompt_lab.run_database_blocking", new=_bounded_recorder(calls)):
            router = self._router()
            self.assertEqual(asyncio.run(_find(router, "/api/v1/analyst-prompt-lab/materialize")()),
                             {"status": "materialized"})
            self.assertEqual(
                asyncio.run(_find(router, "/api/v1/analyst-prompt-lab/candidates/{candidate_id}/label")(
                    uuid4(), AnalystPromptGoldLabelRequest(label="supported", direction_correct=True,
                                                           action_executable=True, reviewer="qa"),
                ))["status"], "labelled",
            )
            self.assertEqual(
                asyncio.run(_find(router, "/api/v1/analyst-prompt-lab/evaluate/{variant_key}")(
                    "strict_action", AnalystPromptEvaluateRequest(minimum_labels=10),
                )),
                {"status": "evaluated"},
            )
            self.assertEqual(
                asyncio.run(_find(router, "/api/v1/analyst-intraday-outcomes/recompute")()),
                {"status": "recomputed"},
            )
        self.assertEqual(len(calls), 4)
        for _action, _args, kwargs in calls:
            self.assertEqual(kwargs, {"timeout_seconds": 3})


class AnalystActionOutcomesRouterBoundaryTests(unittest.TestCase):
    def test_recompute_runs_through_bounded_executor(self) -> None:
        calls: list[tuple[object, tuple, dict]] = []
        router = build_analyst_action_outcomes_router(
            database=_FakeDatabase(), materialize_fn=lambda _connection, cutoff_at: {"status": "recomputed"},
        )
        with patch("app.routers.analyst_action_outcomes.run_database_blocking", new=_bounded_recorder(calls)):
            payload = asyncio.run(_find(router, "/api/v1/analysts/anqiang/trade-action-outcomes/recompute")())
        self.assertEqual(payload, {"status": "recomputed"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], {"timeout_seconds": 3})


class AnalystResearchReviewRunRouterBoundaryTests(unittest.TestCase):
    def test_run_review_runs_through_bounded_executor(self) -> None:
        calls: list[tuple[object, tuple, dict]] = []
        router = build_analyst_research_reads_router(object(), lambda _database, _as_of: {})
        with patch("app.routers.analyst_research_reads.run_database_blocking", new=_bounded_recorder(calls)), \
             patch("app.routers.analyst_research_reads.build_recorded_analyst_market_review",
                   return_value={"cadence": "daily"}):
            endpoint = _find(router, "/api/v1/analyst-research/reviews/run")
            payload = asyncio.run(endpoint(AnalystMarketReviewRequest(cadence="daily")))
        self.assertEqual(payload, {"review": {"cadence": "daily"}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], {"timeout_seconds": 3})


class MarketActionsImportBarsRouterBoundaryTests(unittest.TestCase):
    def test_import_bars_runs_through_bounded_executor(self) -> None:
        calls: list[tuple[object, tuple, dict]] = []

        async def fail(*_args, **_kwargs):
            raise AssertionError("not exercised in this test")

        deps = MarketActionDependencies(
            import_bars=lambda payload: {"imported": len(payload.bars)},
            sync_universe=fail, sync_full_daily=fail, sync_full_daily_controls=fail,
            post_close_refresh=fail, start_post_close_refresh=fail, sync_announcements=fail,
            rebuild_market_flow_features=fail,
        )
        router = build_market_actions_router(deps)
        bar = DailyBar(symbol="600000.SH", trading_date=date(2026, 9, 1), close=Decimal("10.5"))
        with patch("app.routers.market_actions.run_database_blocking", new=_bounded_recorder(calls)):
            payload = asyncio.run(_find(router, "/api/v1/market/bars/import")(BarsImport(bars=[bar])))
        self.assertEqual(payload, {"imported": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], {"timeout_seconds": 10})


class PaperActionsRouterBoundaryTests(unittest.TestCase):
    def test_configure_account_and_accept_decision_run_through_bounded_executor(self) -> None:
        calls: list[tuple[object, tuple, dict]] = []
        router = build_paper_actions_router(
            database=_FakeDatabase(),
            configure_fn=lambda _connection, **_kwargs: {"account_key": "default"},
            accept_fn=lambda _connection, **_kwargs: {"status": "accepted"},
        )
        with patch("app.routers.paper_actions.run_database_blocking", new=_bounded_recorder(calls)):
            configure_payload = asyncio.run(_find(router, "/api/v1/paper/accounts")(
                PaperAccountConfigureRequest(initial_cash=100000, configured_by="qa"),
            ))
            accept_payload = asyncio.run(_find(router, "/api/v1/paper/decisions/{decision_id}/accept")(
                uuid4(), PaperDecisionAcceptRequest(quantity=100),
            ))
        self.assertEqual(configure_payload["account"], {"account_key": "default"})
        self.assertFalse(configure_payload["live_orders"])
        self.assertEqual(accept_payload["status"], "accepted")
        self.assertFalse(accept_payload["live_orders"])
        self.assertEqual(len(calls), 2)
        for _action, _args, kwargs in calls:
            self.assertEqual(kwargs, {"timeout_seconds": 3})


if __name__ == "__main__":
    unittest.main()

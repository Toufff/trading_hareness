"""Read-only strategy result routes."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Literal

from fastapi import APIRouter

from ..strategy_read_model import latest_post_close_strategy as sync_latest_post_close_strategy
from ..strategy_read_model import latest_strategy_decision as sync_latest_strategy_decision
from ..strategy_read_model import latest_strategy_review as sync_latest_strategy_review
from ..async_strategy_read_repository import latest_post_close_strategy, latest_strategy_decision, latest_strategy_review
from ..strategy_ablation import latest_strategy_ablation
from ..async_strategy_ablation_repository import latest_strategy_ablation as async_latest_strategy_ablation
from ..strategy_health_read_model import latest_strategy_health
from ..async_strategy_health_repository import latest_strategy_health as async_latest_strategy_health
from ..strategy_promotion import sync_strategy_promotion_catalog
from ..watchlist_candidate_proposals import sync_latest_watchlist_proposals
from ..strategy_watchlist_projection import compact_post_close_watchlist
from ..strategy_dashboard_projection import dashboard_post_close
from ..intraday_evidence_read_model import watchlists as sync_intraday_watchlists
from ..async_intraday_evidence_read_repository import watchlists as async_intraday_watchlists


def build_strategy_reads_router(database: Any, decision_model_version: str, async_database: Any | None = None,
                                cn_today: Callable[[], date] = date.today) -> APIRouter:
    router = APIRouter(tags=["strategy-reads"])

    @router.get("/api/v1/strategy/events/latest")
    async def event_research_latest() -> dict[str, Any]:
        from ..event_research.async_repository import latest
        if async_database is not None:
            return await latest(async_database)
        from datetime import datetime, timezone
        from ..event_research.pipeline import context
        from ..runtime_executors import run_database_blocking
        return await run_database_blocking(context, database, datetime.now(timezone.utc), timeout_seconds=10)

    @router.get("/api/v1/strategy/decisions/latest")
    async def decision() -> dict[str, Any]:
        if async_database is not None:
            return await latest_strategy_decision(async_database, decision_model_version)
        return sync_latest_strategy_decision(database, decision_model_version)

    @router.get("/api/v1/strategy/reviews/latest")
    async def review(session: Literal["midday", "close"] | None = None) -> dict[str, Any]:
        if async_database is not None:
            return await latest_strategy_review(async_database, session)
        return sync_latest_strategy_review(database, session)

    @router.get("/api/v1/strategy/post-close/latest")
    async def post_close(as_of_date: date | None = None, view: Literal['full', 'dashboard'] = 'full') -> dict[str, Any]:
        if async_database is not None:
            payload = await latest_post_close_strategy(async_database, as_of_date)
        else:
            payload = sync_latest_post_close_strategy(database, as_of_date)
        return dashboard_post_close(payload) if view == 'dashboard' else payload

    @router.get("/api/v1/strategy/post-close/watchlist/latest")
    async def post_close_watchlist(limit: int = 16) -> dict[str, Any]:
        """Small holdings-independent watchlist for human decision surfaces."""
        if async_database is not None:
            payload = await latest_post_close_strategy(async_database)
            tracked = await async_intraday_watchlists(async_database)
        else:
            payload = sync_latest_post_close_strategy(database)
            tracked = sync_intraday_watchlists(database)
        return compact_post_close_watchlist(payload, limit, user_tracking=tracked.get("items") or [])

    @router.get("/api/v1/strategy/ablation/latest")
    async def ablation(limit: int = 200) -> dict[str, Any]:
        if async_database is not None:
            return await async_latest_strategy_ablation(async_database, limit)
        return latest_strategy_ablation(database, limit)

    @router.get("/api/v1/strategy/health")
    async def health() -> dict[str, Any]:
        if async_database is not None:
            return await async_latest_strategy_health(async_database)
        return latest_strategy_health(database)

    @router.get("/api/v1/strategy/promotion")
    async def promotion() -> dict[str, Any]:
        """Fail-closed live-promotion status for every declared strategy contract.

        Mirrors /api/v1/research/analyst-research/... promotion visibility.
        Nothing here can grant execution: it only reports the audit trail a
        human approval would have to leave in quant.strategy_promotion_registry.
        """
        as_of_date = cn_today()
        return {"as_of_date": str(as_of_date), "strategies": sync_strategy_promotion_catalog(database, as_of_date)}

    @router.get("/api/v1/strategy/watchlist-proposals")
    async def watchlist_proposals() -> dict[str, Any]:
        """Cross-strategy watchlist candidates for human review only.

        Never written into quant.intraday_watchlists: that table has a
        previously-verified 40-symbol capacity bound tied to the live
        Tencent batched-quote request size, and a human-curated watchlist
        already uses most of it.
        """
        return sync_latest_watchlist_proposals(database)

    return router

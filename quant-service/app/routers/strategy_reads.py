"""Read-only strategy result routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter

from ..strategy_read_model import latest_post_close_strategy as sync_latest_post_close_strategy
from ..strategy_read_model import latest_strategy_decision as sync_latest_strategy_decision
from ..strategy_read_model import latest_strategy_review as sync_latest_strategy_review
from ..async_strategy_read_repository import latest_post_close_strategy, latest_strategy_decision, latest_strategy_review
from ..strategy_ablation import latest_strategy_ablation
from ..strategy_health_read_model import latest_strategy_health
from ..async_strategy_health_repository import latest_strategy_health as async_latest_strategy_health


def build_strategy_reads_router(database: Any, decision_model_version: str, async_database: Any | None = None) -> APIRouter:
    router = APIRouter(tags=["strategy-reads"])

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
    async def post_close() -> dict[str, Any]:
        if async_database is not None:
            return await latest_post_close_strategy(async_database)
        return sync_latest_post_close_strategy(database)

    @router.get("/api/v1/strategy/ablation/latest")
    def ablation(limit: int = 200) -> dict[str, Any]:
        return latest_strategy_ablation(database, limit)

    @router.get("/api/v1/strategy/health")
    async def health() -> dict[str, Any]:
        if async_database is not None:
            return await async_latest_strategy_health(async_database)
        return latest_strategy_health(database)

    return router

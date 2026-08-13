"""Read-only strategy result routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter

from ..strategy_read_model import latest_post_close_strategy, latest_strategy_decision, latest_strategy_review
from ..strategy_ablation import latest_strategy_ablation
from ..strategy_health_read_model import latest_strategy_health


def build_strategy_reads_router(database: Any, decision_model_version: str) -> APIRouter:
    router = APIRouter(tags=["strategy-reads"])

    @router.get("/api/v1/strategy/decisions/latest")
    def decision() -> dict[str, Any]:
        return latest_strategy_decision(database, decision_model_version)

    @router.get("/api/v1/strategy/reviews/latest")
    def review(session: Literal["midday", "close"] | None = None) -> dict[str, Any]:
        return latest_strategy_review(database, session)

    @router.get("/api/v1/strategy/post-close/latest")
    def post_close() -> dict[str, Any]:
        return latest_post_close_strategy(database)

    @router.get("/api/v1/strategy/ablation/latest")
    def ablation(limit: int = 200) -> dict[str, Any]:
        return latest_strategy_ablation(database, limit)

    @router.get("/api/v1/strategy/health")
    def health() -> dict[str, Any]:
        return latest_strategy_health(database)

    return router

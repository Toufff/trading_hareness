"""Strategy materialization routes with explicit service dependencies.

The routes intentionally do not run SQL, evaluate signals, or create provider
clients.  Services are injected from the application composition root, where
their bounded database execution and market-data gates remain authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..request_models import (
    GenerateRequest,
    PostCloseStrategyRequest,
    StrategyDecisionRequest,
    StrategyPatternMiningRequest,
    StrategyReviewRequest,
)


@dataclass(frozen=True)
class StrategyActionDependencies:
    decision: Callable[[StrategyDecisionRequest], Awaitable[dict[str, Any]]]
    review: Callable[[StrategyReviewRequest], Awaitable[dict[str, Any]]]
    post_close: Callable[[PostCloseStrategyRequest], Awaitable[dict[str, Any]]]
    pattern_mining: Callable[[StrategyPatternMiningRequest], Awaitable[dict[str, Any]]]
    recompute_scorecards: Callable[[date | None], Awaitable[dict[str, Any]]]
    recompute_outcomes: Callable[[date | None], Awaitable[dict[str, Any]]]
    recompute_intraday_outcomes: Callable[[date | None], Awaitable[dict[str, Any]]]
    generate_recommendations: Callable[[GenerateRequest], Awaitable[dict[str, Any]]]
    daily_pipeline: Callable[[GenerateRequest], Awaitable[dict[str, Any]]]


def build_strategy_actions_router(deps: StrategyActionDependencies) -> APIRouter:
    """Build stable strategy write routes without application globals."""
    router = APIRouter(tags=["strategy-actions"])

    @router.post("/api/v1/strategy/decisions/run")
    async def decision(payload: StrategyDecisionRequest) -> dict[str, Any]:
        return await deps.decision(payload)

    @router.post("/api/v1/strategy/reviews/run")
    async def review(payload: StrategyReviewRequest) -> dict[str, Any]:
        return await deps.review(payload)

    @router.post("/api/v1/strategy/post-close/run")
    async def post_close(payload: PostCloseStrategyRequest) -> dict[str, Any]:
        return await deps.post_close(payload)

    @router.post("/api/v1/strategy/pattern-mining/run")
    async def pattern_mining(payload: StrategyPatternMiningRequest) -> dict[str, Any]:
        return await deps.pattern_mining(payload)

    @router.post("/api/v1/analyst-scorecards/recompute")
    async def scorecards(as_of_date: date | None = None) -> dict[str, Any]:
        return await deps.recompute_scorecards(as_of_date)

    @router.post("/api/v1/outcomes/recompute")
    async def outcomes(as_of_date: date | None = None) -> dict[str, Any]:
        return await deps.recompute_outcomes(as_of_date)

    @router.post("/api/v1/intraday/outcomes/recompute")
    async def intraday_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
        return await deps.recompute_intraday_outcomes(as_of_date)

    @router.post("/api/v1/recommendations/generate")
    async def recommendations(payload: GenerateRequest) -> dict[str, Any]:
        return await deps.generate_recommendations(payload)

    @router.post("/api/v1/pipeline/daily")
    async def daily_pipeline(payload: GenerateRequest) -> dict[str, Any]:
        return await deps.daily_pipeline(payload)

    return router


__all__ = ["StrategyActionDependencies", "build_strategy_actions_router"]

"""HTTP boundary for the research-only 小杰龙头策略 evaluator."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..request_models import XiaojieLeaderFlowEvaluateRequest


def build_xiaojie_leader_flow_router(
    evaluate_fn: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["xiaojie-leader-flow"])

    @router.post("/api/v1/research/strategies/xiaojie-leader-flow/evaluate")
    def evaluate(payload: XiaojieLeaderFlowEvaluateRequest) -> dict[str, Any]:
        result = evaluate_fn(payload.snapshot.model_dump(exclude_none=True), payload.parameters)
        return {**result, "live_effect": "none", "boundary": "research_only; no_automatic_order"}

    return router


__all__ = ["build_xiaojie_leader_flow_router"]

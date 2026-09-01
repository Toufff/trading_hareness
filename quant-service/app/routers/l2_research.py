"""HTTP boundary for the fail-closed Level-2 research gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..request_models import L2IncrementalEvaluationRequest


@dataclass(frozen=True)
class L2ResearchDependencies:
    record: Callable[[L2IncrementalEvaluationRequest], Awaitable[dict[str, Any]]]
    latest: Callable[[], Awaitable[dict[str, Any]]]


def build_l2_research_router(deps: L2ResearchDependencies) -> APIRouter:
    router = APIRouter(tags=["l2-research"])

    @router.post("/api/v1/research/l2/evaluations")
    async def record(payload: L2IncrementalEvaluationRequest) -> dict[str, Any]:
        return await deps.record(payload)

    @router.get("/api/v1/research/l2/evaluations/latest")
    async def latest() -> dict[str, Any]:
        return await deps.latest()

    return router


__all__ = ["L2ResearchDependencies", "build_l2_research_router"]

"""Write route for the isolated ten-day leader-rotation materializer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..ten_day_leader_rotation_contracts import TenDayLeaderRotationRunRequest


@dataclass(frozen=True)
class TenDayLeaderRotationActionDependencies:
    run: Callable[[TenDayLeaderRotationRunRequest], Awaitable[dict[str, Any]]]


def build_ten_day_leader_rotation_actions_router(
    dependencies: TenDayLeaderRotationActionDependencies,
) -> APIRouter:
    router = APIRouter(tags=["ten-day-leader-rotation-actions"])

    @router.post("/api/v1/research/ten-day-leader-rotation/run")
    async def run(payload: TenDayLeaderRotationRunRequest) -> dict[str, Any]:
        return await dependencies.run(payload)

    return router


__all__ = ["TenDayLeaderRotationActionDependencies", "build_ten_day_leader_rotation_actions_router"]

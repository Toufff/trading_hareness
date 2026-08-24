"""Read route for the isolated ten-day leader-rotation projection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..ten_day_leader_rotation_contracts import TenDayLeaderRotationLatestResponse
from ..ten_day_leader_rotation_read_repository import latest_ten_day_leader_rotation


def build_ten_day_leader_rotation_reads_router(async_database: Any) -> APIRouter:
    router = APIRouter(tags=["ten-day-leader-rotation-reads"])

    @router.get("/api/v1/research/ten-day-leader-rotation/latest", response_model=TenDayLeaderRotationLatestResponse)
    async def latest(limit: int = Query(default=90, ge=1, le=90)) -> dict[str, Any]:
        return await latest_ten_day_leader_rotation(async_database, limit=limit)

    return router


__all__ = ["build_ten_day_leader_rotation_reads_router"]

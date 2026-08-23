"""Read-only API assembly for board-rotation evidence."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_board_research_read_repository import latest_board_rotation_events as async_latest_board_rotation_events
from ..board_rotation_read_model import latest_board_rotation_events


def build_board_rotation_reads_router(
    database: Any,
    *,
    async_database: Any | None = None,
    async_events_fn: Callable[[Any, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["board-rotation-reads"])

    @router.get("/api/v1/intraday/board-rotations/latest")
    async def latest_board_rotations(limit: int = 30) -> dict[str, Any]:
        if async_database is not None:
            return await (async_events_fn or async_latest_board_rotation_events)(async_database, limit)
        return latest_board_rotation_events(database, limit)

    return router

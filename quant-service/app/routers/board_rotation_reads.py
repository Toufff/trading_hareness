"""Read-only API assembly for board-rotation evidence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..board_rotation_read_model import latest_board_rotation_events


def build_board_rotation_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["board-rotation-reads"])

    @router.get("/api/v1/intraday/board-rotations/latest")
    def latest_board_rotations(limit: int = 30) -> dict[str, Any]:
        return latest_board_rotation_events(database, limit)

    return router

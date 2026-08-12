"""Read-only route for board-flow stock-mining candidates."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..board_stock_mining_read_model import latest_board_stock_mining


def build_board_stock_mining_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["board-stock-mining-reads"])

    @router.get("/api/v1/intraday/board-stock-mining/latest")
    def latest_board_stock_mining_candidates(limit: int = 20) -> dict[str, Any]:
        return latest_board_stock_mining(database, limit)

    return router

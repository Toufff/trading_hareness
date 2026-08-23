"""Read-only route for board-flow stock-mining candidates."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_board_research_read_repository import latest_board_stock_mining as async_latest_board_stock_mining
from ..board_stock_mining_read_model import latest_board_stock_mining


def build_board_stock_mining_reads_router(
    database: Any,
    *,
    async_database: Any | None = None,
    async_mining_fn: Callable[[Any, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["board-stock-mining-reads"])

    @router.get("/api/v1/intraday/board-stock-mining/latest")
    async def latest_board_stock_mining_candidates(limit: int = 20) -> dict[str, Any]:
        if async_database is not None:
            return await (async_mining_fn or async_latest_board_stock_mining)(async_database, limit)
        return latest_board_stock_mining(database, limit)

    return router

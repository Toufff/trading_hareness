"""Read-only route for live limit-up linkage research candidates."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_limit_linkage_mining_read_repository import latest_limit_linkage_mining as async_latest_limit_linkage_mining
from ..limit_linkage_mining_read_model import latest_limit_linkage_mining


def build_limit_linkage_mining_reads_router(
    database: Any,
    *,
    async_database: Any | None = None,
    async_linkage_fn: Callable[[Any, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["limit-linkage-mining-reads"])

    @router.get("/api/v1/intraday/limit-linkage/latest")
    async def latest_limit_linkage(limit: int = 30) -> dict[str, Any]:
        if async_database is not None:
            return await (async_linkage_fn or async_latest_limit_linkage_mining)(async_database, limit)
        return latest_limit_linkage_mining(database, limit)

    return router

"""Read-only analyst action replay endpoint."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_analyst_action_read_repository import anqiang_trade_action_replay as async_anqiang_trade_action_replay


def build_analyst_trade_action_reads_router(
    database: Any,
    replay_fn: Callable[[Any, date | None, int], dict[str, Any]],
    *,
    async_database: Any | None = None,
    async_replay_fn: Callable[[Any, date | None, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["analyst-trade-action-reads"])

    @router.get("/api/v1/analysts/anqiang/trade-actions")
    async def replay(as_of_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        if async_database is not None:
            return await (async_replay_fn or async_anqiang_trade_action_replay)(async_database, as_of_date, limit)
        return replay_fn(database, as_of_date, limit)

    return router

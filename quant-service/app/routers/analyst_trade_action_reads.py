"""Read-only analyst action replay endpoint."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from fastapi import APIRouter


def build_analyst_trade_action_reads_router(database: Any, replay_fn: Callable[[Any, date | None, int], dict[str, Any]]) -> APIRouter:
    router = APIRouter(tags=["analyst-trade-action-reads"])

    @router.get("/api/v1/analysts/anqiang/trade-actions")
    def replay(as_of_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        return replay_fn(database, as_of_date, limit)

    return router

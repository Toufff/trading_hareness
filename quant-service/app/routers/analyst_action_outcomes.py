"""Replay-only read and recompute endpoints for author-stated Anqiang actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_analyst_action_read_repository import anqiang_trade_action_outcomes as async_anqiang_trade_action_outcomes
from ..runtime_executors import run_database_blocking


def _status_sync(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT methodology_version,horizon_minutes,status,count(*)::int AS count,
                      avg(directional_return) AS avg_directional_return
                 FROM quant.analyst_action_intraday_outcomes
                GROUP BY methodology_version,horizon_minutes,status
                ORDER BY methodology_version,horizon_minutes,status"""
        ).fetchall()
    return {"analyst_id": "anqiang-touzi-riji", "outcomes": [dict(row) for row in rows],
            "data_boundary": "author-stated-time retrospective replay only; no live strategy effect"}


def build_analyst_action_outcomes_router(
    database: Any,
    materialize_fn: Callable[..., dict[str, Any]],
    *,
    async_database: Any | None = None,
    async_outcomes_fn: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["analyst-action-outcomes"])

    @router.get("/api/v1/analysts/anqiang/trade-action-outcomes")
    async def status() -> dict[str, Any]:
        if async_database is not None:
            return await (async_outcomes_fn or async_anqiang_trade_action_outcomes)(async_database)
        return _status_sync(database)

    @router.post("/api/v1/analysts/anqiang/trade-action-outcomes/recompute")
    async def recompute() -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with database.transaction() as connection:
                return materialize_fn(connection, cutoff_at=datetime.now(timezone.utc))
        return await run_database_blocking(run, timeout_seconds=3)

    return router


__all__ = ["build_analyst_action_outcomes_router"]

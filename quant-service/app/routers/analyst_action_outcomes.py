"""Replay-only read and recompute endpoints for author-stated Anqiang actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter


def build_analyst_action_outcomes_router(database: Any, materialize_fn: Callable[..., dict[str, Any]]) -> APIRouter:
    router = APIRouter(tags=["analyst-action-outcomes"])

    @router.get("/api/v1/analysts/anqiang/trade-action-outcomes")
    def status() -> dict[str, Any]:
        with database.transaction() as connection:
            rows = connection.execute(
                """SELECT methodology_version,horizon_minutes,status,count(*)::int AS count,
                          avg(directional_return) AS avg_directional_return
                     FROM quant.analyst_action_intraday_outcomes
                    GROUP BY methodology_version,horizon_minutes,status
                    ORDER BY methodology_version,horizon_minutes,status"""
            ).fetchall()
        return {
            "analyst_id": "anqiang-touzi-riji", "outcomes": [dict(row) for row in rows],
            "data_boundary": "author-stated-time retrospective replay only; no live strategy effect",
        }

    @router.post("/api/v1/analysts/anqiang/trade-action-outcomes/recompute")
    def recompute() -> dict[str, Any]:
        with database.transaction() as connection:
            return materialize_fn(connection, cutoff_at=datetime.now(timezone.utc))

    return router


__all__ = ["build_analyst_action_outcomes_router"]

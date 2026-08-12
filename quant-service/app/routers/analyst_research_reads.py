"""Read-only analyst research status."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from fastapi import APIRouter


def build_analyst_research_reads_router(database: Any, status_fn: Callable[[Any, date | None], dict[str, Any]]) -> APIRouter:
    router = APIRouter(tags=["analyst-research-reads"])

    @router.get("/api/v1/analyst-research/status")
    def status(as_of_date: date | None = None) -> dict[str, Any]:
        return status_fn(database, as_of_date)

    @router.get("/api/v1/analyst-research/profiles")
    def profiles() -> dict[str, Any]:
        with database.transaction() as connection:
            rows = connection.execute(
                """SELECT a.remote_analyst_id,a.name,p.independence_class,p.audience_size,p.audience_as_of,p.evidence,p.updated_at
                     FROM quant.remote_analysts a LEFT JOIN quant.analyst_research_profiles p USING(remote_analyst_id)
                     ORDER BY a.remote_analyst_id"""
            ).fetchall()
        return {"items": [dict(row) for row in rows], "boundary": "manual provenance only; it is an explicit prior, not inferred from outcomes"}

    return router

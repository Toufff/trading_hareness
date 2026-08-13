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

    @router.get("/api/v1/analyst-research/observations")
    def observations(analyst_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 500))
        with database.transaction() as connection:
            rows = connection.execute(
                """SELECT observation_id,analyst_id,source_kind,source_id,source_version,content_hash,
                          strategy_available_at,published_at,stated_at,scope,subject_key,subject_label,
                          action,direction,horizon_days,strength,confidence,conditions,evidence_span,
                          extractor_version,status,created_at
                     FROM quant.analyst_observations
                    WHERE (%s::text IS NULL OR analyst_id=%s)
                    ORDER BY strategy_available_at DESC LIMIT %s""",
                (analyst_id, analyst_id, bounded),
            ).fetchall()
            health = connection.execute(
                """SELECT analyst_id,count(*)::int observations,
                          count(*) FILTER (WHERE status='eligible')::int eligible,
                          count(*) FILTER (WHERE status='replay_only')::int replay_only,
                          max(strategy_available_at) latest_available_at
                     FROM quant.analyst_observations
                    WHERE (%s::text IS NULL OR analyst_id=%s)
                    GROUP BY analyst_id ORDER BY analyst_id""", (analyst_id, analyst_id),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "health": [dict(row) for row in health],
                "live_effect": "none", "boundary": "append_only text-derived observations; promotion registry remains zero by default"}

    @router.get("/api/v1/analyst-research/sync-health")
    def sync_health() -> dict[str, Any]:
        with database.transaction() as connection:
            cursors = connection.execute(
                """SELECT stream_key,remote_analyst_id,received_at,message_ids,report_versions,updated_at
                     FROM quant.analyst_sync_cursors ORDER BY updated_at DESC"""
            ).fetchall()
            promotion = connection.execute(
                """SELECT promotion_key,methodology_version,status,max_live_weight,approved_by,approved_at,reason,updated_at
                     FROM quant.analyst_promotion_registry ORDER BY promotion_key"""
            ).fetchall()
        return {"cursors": [dict(row) for row in cursors], "promotion_registry": [dict(row) for row in promotion],
                "live_effect": "none_until_explicit_approval", "boundary": "remote sync health is read-only"}

    return router

"""Read-only analyst research status."""

from __future__ import annotations

from datetime import date, datetime, timezone
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
        workflow_health: list[dict[str, Any]] = []
        with database.transaction() as connection:
            cursors = connection.execute(
                """SELECT stream_key,remote_analyst_id,received_at,message_ids,report_versions,updated_at
                     FROM quant.analyst_sync_cursors ORDER BY updated_at DESC"""
            ).fetchall()
            global_cursors = connection.execute(
                """SELECT stream_key,remote_cursor,received_after,updated_at
                     FROM quant.analyst_global_sync_cursors ORDER BY stream_key"""
            ).fetchall()
            promotion = connection.execute(
                """SELECT promotion_key,methodology_version,status,max_live_weight,approved_by,approved_at,reason,updated_at
                     FROM quant.analyst_promotion_registry ORDER BY promotion_key"""
            ).fetchall()
            try:
                workflow_rows = connection.execute(
                    """SELECT w.id,w.active,w."activeVersionId" AS active_version_id,
                                  (w."activeVersionId" IS NOT NULL
                                   AND w."activeVersionId"=p."publishedVersionId") AS published,
                                  e.status AS latest_execution_status,e."startedAt" AS latest_started_at,
                                  e."stoppedAt" AS latest_stopped_at,
                                  e."workflowVersionId" AS latest_execution_version_id
                             FROM public.workflow_entity w
                        LEFT JOIN public.workflow_published_version p ON p."workflowId"=w.id
                        LEFT JOIN LATERAL (
                            SELECT status,"startedAt","stoppedAt","workflowVersionId"
                              FROM public.execution_entity
                             WHERE "workflowId"=w.id AND "deletedAt" IS NULL
                             ORDER BY "startedAt" DESC NULLS LAST,id DESC LIMIT 1
                        ) e ON TRUE
                            WHERE w.id IN ('remoteArchiveReports123','remoteArchiveMessages123')
                            ORDER BY w.id"""
                ).fetchall()
                workflow_health = []
                for row in workflow_rows:
                    item = dict(row)
                    active_current = bool(item.get("active") and item.get("published"))
                    execution_matches_active = bool(
                        item.get("latest_execution_version_id")
                        and item.get("latest_execution_version_id") == item.get("active_version_id")
                    )
                    execution_succeeded = execution_matches_active and item.get("latest_execution_status") == "success"
                    if not active_current:
                        item["status"] = "disabled"
                        item["execution_evidence"] = "none"
                        item["notice"] = "workflow is not active and published"
                    elif execution_succeeded:
                        item["status"] = "ready"
                        item["execution_evidence"] = "current_workflow_execution"
                        item["notice"] = None
                    elif execution_matches_active:
                        item["status"] = "degraded"
                        item["execution_evidence"] = "current_workflow_execution_failed"
                        item["notice"] = "current published workflow has not completed successfully"
                    else:
                        # Existing cursors prove prior service imports, but a
                        # retired execution cannot validate the current graph.
                        item["status"] = "pending_first_current_execution"
                        item["execution_evidence"] = "service_cursor_prior_version"
                        item["notice"] = "awaiting the first successful execution of the current published workflow"
                    workflow_health.append(item)
            except Exception:
                # The quant schema can be deployed without n8n's public schema
                # in an isolated environment; sync evidence remains usable.
                workflow_health = []
        now = datetime.now(timezone.utc)
        stream_health: list[dict[str, Any]] = []
        for stream_key in ("reports", "messages"):
            stream_rows = [dict(row) for row in cursors if row["stream_key"] == stream_key]
            if stream_key == "messages":
                stream_rows.extend(dict(row) for row in global_cursors if row["stream_key"] == "message_updates")
            latest = max((row.get("updated_at") for row in stream_rows if row.get("updated_at")), default=None)
            age_seconds = None
            if latest is not None:
                latest_at = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (now - latest_at).total_seconds())
            status = "never_succeeded" if latest is None else "stale" if age_seconds > 24 * 3600 else "ready"
            stream_health.append({
                "stream_key": stream_key,
                "status": status,
                "cursor_count": len(stream_rows),
                "latest_cursor_at": latest,
                "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                "expected_workflow_id": "remoteArchiveMessages123" if stream_key == "messages" else "remoteArchiveReports123",
                "notice": "no successful cursor advance is recorded" if latest is None else None,
            })
        streams_ready = all(item.get("status") == "ready" for item in stream_health)
        ready_workflows = {str(item.get("id")) for item in workflow_health if item.get("status") == "ready"}
        workflow_verified = streams_ready and {"remoteArchiveReports123", "remoteArchiveMessages123"}.issubset(ready_workflows)
        return {"cursors": [dict(row) for row in cursors], "stream_health": stream_health,
                "workflow_health": workflow_health,
                "promotion_registry": [dict(row) for row in promotion],
                "live_effect": "none_until_explicit_approval", "boundary": "remote sync health is read-only",
                "runtime_verification": "verified_recent_execution" if workflow_verified else "pending_next_scheduled_execution"}

    return router

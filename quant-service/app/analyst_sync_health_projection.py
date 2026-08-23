"""Shared query contracts and pure projection for analyst-sync health."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


CURSORS_SQL = """SELECT stream_key,remote_analyst_id,received_at,message_ids,report_versions,updated_at
                     FROM quant.analyst_sync_cursors ORDER BY updated_at DESC"""
GLOBAL_CURSORS_SQL = """SELECT stream_key,remote_cursor,received_after,updated_at
                     FROM quant.analyst_global_sync_cursors ORDER BY stream_key"""
ATTEMPTS_SQL = """SELECT DISTINCT ON (stream_key) stream_key,status,started_at,completed_at,error_code,summary
                     FROM quant.analyst_sync_attempts
                    ORDER BY stream_key,completed_at DESC,attempt_id DESC"""
PROMOTION_SQL = """SELECT promotion_key,methodology_version,status,max_live_weight,approved_by,approved_at,reason,updated_at
                     FROM quant.analyst_promotion_registry ORDER BY promotion_key"""
WORKFLOW_SQL = """SELECT w.id,w.active,w."activeVersionId" AS active_version_id,
                          (w."activeVersionId" IS NOT NULL
                           AND w."activeVersionId"=p."publishedVersionId") AS published,
                          e.status AS latest_execution_status,e."startedAt" AS latest_started_at,
                          e."stoppedAt" AS latest_stopped_at,
                          e."workflowVersionId" AS latest_execution_version_id,
                          smoke.status AS smoke_execution_status,
                          smoke."stoppedAt" AS smoke_execution_at,
                          smoke."workflowVersionId" AS smoke_execution_version_id
                     FROM public.workflow_entity w
                LEFT JOIN public.workflow_published_version p ON p."workflowId"=w.id
                LEFT JOIN LATERAL (
                    SELECT status,"startedAt","stoppedAt","workflowVersionId"
                      FROM public.execution_entity
                     WHERE "workflowId"=w.id AND "deletedAt" IS NULL
                       AND mode='trigger'
                     ORDER BY "startedAt" DESC NULLS LAST,id DESC LIMIT 1
                ) e ON TRUE
                LEFT JOIN LATERAL (
                    SELECT status,"stoppedAt","workflowVersionId"
                      FROM public.execution_entity
                     WHERE "workflowId"=w.id
                       AND mode='cli' AND status='success' AND finished=true
                       AND "workflowVersionId"=w."activeVersionId"
                     ORDER BY "stoppedAt" DESC NULLS LAST,id DESC LIMIT 1
                ) smoke ON TRUE
                    WHERE w.id IN ('remoteArchiveReports123','remoteArchiveMessages123')
                    ORDER BY w.id"""

REPORTS_WORKFLOW_ID = "remoteArchiveReports123"
MESSAGES_WORKFLOW_ID = "remoteArchiveMessages123"


def project_sync_health(
    cursors: Iterable[Any],
    global_cursors: Iterable[Any],
    attempts: Iterable[Any],
    promotion: Iterable[Any],
    workflow_rows: Iterable[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create the read-only health envelope from already-fetched evidence."""
    cursor_items = [dict(row) for row in cursors]
    global_cursor_items = [dict(row) for row in global_cursors]
    attempt_items = [dict(row) for row in attempts]
    promotion_items = [dict(row) for row in promotion]
    workflow_health: list[dict[str, Any]] = []
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
        elif item.get("smoke_execution_version_id") == item.get("active_version_id"):
            item["status"] = "degraded"
            item["execution_evidence"] = "current_workflow_cli_smoke"
            item["notice"] = "current graph passed CLI smoke; awaiting the first scheduled trigger"
        else:
            item["status"] = "pending_first_current_execution"
            item["execution_evidence"] = "service_cursor_prior_version"
            item["notice"] = "awaiting the first successful execution of the current published workflow"
        workflow_health.append(item)

    timestamp = now or datetime.now(timezone.utc)
    latest_attempts = {
        str(item.get("stream_key")): item for item in attempt_items
        if str(item.get("stream_key") or "") in {"reports", "messages"}
    }
    for item in workflow_health:
        stream_key = "messages" if str(item.get("id")) == MESSAGES_WORKFLOW_ID else "reports"
        attempt = latest_attempts.get(stream_key) or {}
        summary = attempt.get("summary") if isinstance(attempt.get("summary"), dict) else {}
        if (
            item.get("active") and item.get("published")
            and attempt.get("status") == "completed"
            and summary.get("workflow_id") == item.get("id")
        ):
            item["status"] = "ready"
            item["execution_evidence"] = "current_workflow_sync_receipt"
            item["smoke_execution_status"] = "success"
            item["smoke_execution_at"] = attempt.get("completed_at")
            item["notice"] = None

    stream_health: list[dict[str, Any]] = []
    for stream_key in ("reports", "messages"):
        stream_rows = [row for row in cursor_items if row["stream_key"] == stream_key]
        if stream_key == "messages":
            stream_rows.extend(row for row in global_cursor_items if row["stream_key"] == "message_updates")
        latest = max((row.get("updated_at") for row in stream_rows if row.get("updated_at")), default=None)
        age_seconds = None
        if latest is not None:
            latest_at = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (timestamp - latest_at).total_seconds())
        attempt = latest_attempts.get(stream_key)
        attempt_at = attempt.get("completed_at") if attempt else None
        attempt_age_seconds = None
        if attempt_at is not None:
            attempt_timestamp = attempt_at if attempt_at.tzinfo else attempt_at.replace(tzinfo=timezone.utc)
            attempt_age_seconds = max(0.0, (timestamp - attempt_timestamp).total_seconds())
        attempt_is_recent_success = bool(
            attempt and attempt.get("status") == "completed"
            and attempt_age_seconds is not None and attempt_age_seconds <= 24 * 3600
        )
        status = (
            "ready" if attempt_is_recent_success else
            "never_succeeded" if latest is None else
            "stale" if age_seconds > 24 * 3600 else "ready"
        )
        notice = None
        if attempt and not attempt_is_recent_success and attempt.get("status") == "failed":
            notice = f"latest sync attempt failed: {str(attempt.get('error_code') or 'unknown')}"
        elif latest is None and not attempt_is_recent_success:
            notice = "no successful cursor advance or recent completed sync is recorded"
        stream_health.append({
            "stream_key": stream_key, "status": status, "cursor_count": len(stream_rows),
            "latest_cursor_at": latest, "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "latest_attempt_at": attempt_at,
            "attempt_age_seconds": round(attempt_age_seconds, 1) if attempt_age_seconds is not None else None,
            "latest_attempt_status": attempt.get("status") if attempt else None,
            "latest_attempt_error_code": attempt.get("error_code") if attempt else None,
            "latest_attempt_summary": attempt.get("summary") if attempt else None,
            "expected_workflow_id": MESSAGES_WORKFLOW_ID if stream_key == "messages" else REPORTS_WORKFLOW_ID,
            "notice": notice,
        })
    streams_ready = all(item.get("status") == "ready" for item in stream_health)
    ready_workflows = {str(item.get("id")) for item in workflow_health if item.get("status") == "ready"}
    smoke_workflows = {
        str(item.get("id")) for item in workflow_health
        if item.get("execution_evidence") == "current_workflow_cli_smoke"
    }
    expected_workflows = {REPORTS_WORKFLOW_ID, MESSAGES_WORKFLOW_ID}
    if streams_ready and expected_workflows.issubset(ready_workflows):
        runtime_verification = "verified_recent_execution"
    elif streams_ready and expected_workflows.issubset(smoke_workflows):
        runtime_verification = "verified_cli_smoke_pending_scheduled_execution"
    elif streams_ready:
        runtime_verification = "service_reachable_pending_scheduled_execution"
    else:
        runtime_verification = "pending_next_scheduled_execution"
    return {
        "cursors": cursor_items, "stream_health": stream_health, "workflow_health": workflow_health,
        "promotion_registry": promotion_items, "live_effect": "none_until_explicit_approval",
        "boundary": "remote sync health is read-only", "runtime_verification": runtime_verification,
    }


__all__ = [
    "ATTEMPTS_SQL", "CURSORS_SQL", "GLOBAL_CURSORS_SQL", "MESSAGES_WORKFLOW_ID", "PROMOTION_SQL",
    "REPORTS_WORKFLOW_ID", "WORKFLOW_SQL", "project_sync_health",
]

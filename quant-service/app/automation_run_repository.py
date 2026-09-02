"""Small persistence boundary for scheduled task execution evidence."""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Json


LATEST_RUNS_SQL = """SELECT run_id,task_key,run_key,cadence,as_of_date,status,methodology_version,
                      input_summary,output_summary,error_class,error_message,started_at,finished_at,updated_at
                 FROM quant.automation_runs
                WHERE (%s::text IS NULL OR task_key=%s)
                ORDER BY started_at DESC LIMIT %s"""

#: Shared identity for post-close strategy generation, regardless of which
#: trigger runs it (audit section B, HIGH): the scheduled loop
#: (strategy_runtime_runners.py) and the one-click manual refresh
#: (post_close_refresh_service.py) previously used different task_key/run_key
#: values, so during their 18:55-20:30 overlap window neither trigger's
#: durable dedup in run_recorded could see the other already working on the
#: same exchange date, and both could write the same strategy tables at once.
POST_CLOSE_STRATEGY_TASK_KEY = "post_close_strategy"
POST_CLOSE_STRATEGY_RUN_KEY_PREFIX = "post-close-strategy"


def post_close_strategy_run_key(as_of_date: date) -> str:
    """Build the one run_key every post-close strategy trigger must share."""
    return f"{POST_CLOSE_STRATEGY_RUN_KEY_PREFIX}:{as_of_date}"


def latest_runs_params(task_key: str | None, limit: int) -> tuple[tuple[Any, ...], int]:
    """Bound the shared automation receipt query for sync and async readers."""
    bounded = max(1, min(int(limit), 100))
    return (task_key, task_key, bounded), bounded


def start_run(connection: Any, *, task_key: str, run_key: str, cadence: str | None = None,
              as_of_date: date | None = None, methodology_version: str | None = None,
              input_summary: dict[str, Any] | None = None) -> str:
    row = connection.execute(
        """INSERT INTO quant.automation_runs(task_key,run_key,cadence,as_of_date,status,methodology_version,input_summary)
             VALUES(%s,%s,%s,%s,'running',%s,%s)
        ON CONFLICT(run_key) DO UPDATE SET status='running',error_class=NULL,error_message=NULL,
             finished_at=NULL,updated_at=now(),input_summary=EXCLUDED.input_summary
        RETURNING run_id""",
        (task_key, run_key, cadence, as_of_date, methodology_version, Json(input_summary or {})),
    ).fetchone()
    return str(row["run_id"])


def start_or_resume_run(connection: Any, *, task_key: str, run_key: str, cadence: str | None = None,
                        as_of_date: date | None = None, methodology_version: str | None = None,
                        input_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a durable run, preserving a completed receipt across retries.

    A process restart or a recovered network must not repeat a stage whose
    side effects were already committed.  Failed/partial runs are reopened;
    completed runs remain completed and return their bounded output summary.
    The unique ``run_key`` makes this safe for the stage-level idempotency
    keys used by the post-close orchestrator.
    """
    row = connection.execute(
        """INSERT INTO quant.automation_runs(task_key,run_key,cadence,as_of_date,status,methodology_version,input_summary)
             VALUES(%s,%s,%s,%s,'running',%s,%s)
        ON CONFLICT(run_key) DO UPDATE SET
             status=CASE WHEN quant.automation_runs.status='completed' THEN 'completed' ELSE 'running' END,
             error_class=CASE WHEN quant.automation_runs.status='completed' THEN quant.automation_runs.error_class ELSE NULL END,
             error_message=CASE WHEN quant.automation_runs.status='completed' THEN quant.automation_runs.error_message ELSE NULL END,
             finished_at=CASE WHEN quant.automation_runs.status='completed' THEN quant.automation_runs.finished_at ELSE NULL END,
             updated_at=now(), input_summary=EXCLUDED.input_summary
        RETURNING run_id,status,output_summary""",
        (task_key, run_key, cadence, as_of_date, methodology_version, Json(input_summary or {})),
    ).fetchone()
    return {"run_id": str(row["run_id"]), "status": str(row["status"]), "output_summary": row.get("output_summary") or {}}


def finish_run(connection: Any, run_id: str, *, status: str = "completed",
               output_summary: dict[str, Any] | None = None) -> None:
    connection.execute(
        """UPDATE quant.automation_runs
              SET status=%s,output_summary=%s,finished_at=now(),updated_at=now()
            WHERE run_id=%s""",
        (status, Json(output_summary or {}), run_id),
    )


def fail_run(connection: Any, run_id: str, error: BaseException, *, error_class: str = "task_error") -> None:
    connection.execute(
        """UPDATE quant.automation_runs
              SET status='failed',error_class=%s,error_message=%s,finished_at=now(),updated_at=now()
            WHERE run_id=%s""",
        (error_class, str(error)[:500], run_id),
    )


def latest_runs(connection: Any, *, task_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    params, _ = latest_runs_params(task_key, limit)
    rows = connection.execute(LATEST_RUNS_SQL, params).fetchall()
    return [dict(row) for row in rows]


#: A run_key still ``status='running'`` and updated within this window is
#: treated as genuinely in-flight elsewhere; a scheduler retry must skip it
#: rather than start a second concurrent execution of the same work.
DEFAULT_STALE_RUNNING_MINUTES = 10


def run_recorded(database: Any, *, task_key: str, run_key: str, operation: Any,
                 cadence: str | None = None, as_of_date: date | None = None,
                 methodology_version: str | None = None,
                 input_summary: dict[str, Any] | None = None,
                 in_flight_run_keys: set[str] | None = None,
                 stale_running_minutes: int = DEFAULT_STALE_RUNNING_MINUTES) -> Any:
    """Execute one synchronous task while recording durable lifecycle state.

    Three layers guard against a scheduler racing itself into running the
    same work twice (audit section B, HIGH: ``completed_for_date`` read then
    ``run_recorded`` write was not atomic, and repeated timeouts could stack
    up concurrent executions competing for the same bounded database
    workers):

    1. ``start_or_resume_run`` (not ``start_run``) so a completed receipt is
       returned as-is instead of being overwritten back to ``running``.
    2. A durable check: a run_key already ``status='running'`` and updated
       within ``stale_running_minutes`` is treated as in-flight elsewhere and
       skipped, rather than started a second time.
    3. An optional process-local ``in_flight_run_keys`` set catches a
       concurrent call to this function for the same run_key within one
       process before it even reaches the database.
    """
    if in_flight_run_keys is not None:
        if run_key in in_flight_run_keys:
            return {"status": "skipped_in_flight_process"}
        in_flight_run_keys.add(run_key)
    try:
        with database.transaction() as connection:
            still_running = connection.execute(
                """SELECT 1 FROM quant.automation_runs
                    WHERE run_key=%s AND status='running'
                      AND updated_at > now() - (%s * interval '1 minute')""",
                (run_key, stale_running_minutes),
            ).fetchone()
            if still_running:
                return {"status": "skipped_running_elsewhere"}
            resumed = start_or_resume_run(
                connection, task_key=task_key, run_key=run_key, cadence=cadence,
                as_of_date=as_of_date, methodology_version=methodology_version,
                input_summary=input_summary,
            )
        if resumed["status"] == "completed":
            summary = resumed.get("output_summary")
            resumed_result = dict(summary) if isinstance(summary, dict) else {}
            resumed_result.setdefault("status", "completed")
            return resumed_result
        run_id = resumed["run_id"]
        try:
            result = operation()
        except Exception as error:
            with database.transaction() as connection:
                fail_run(connection, run_id, error)
            raise
        with database.transaction() as connection:
            finish_run(connection, run_id, output_summary={"status": result.get("status")} if isinstance(result, dict) else {})
        return result
    finally:
        if in_flight_run_keys is not None:
            in_flight_run_keys.discard(run_key)


__all__ = [
    "DEFAULT_STALE_RUNNING_MINUTES", "LATEST_RUNS_SQL", "POST_CLOSE_STRATEGY_RUN_KEY_PREFIX",
    "POST_CLOSE_STRATEGY_TASK_KEY", "post_close_strategy_run_key",
    "start_run", "start_or_resume_run", "finish_run", "fail_run",
    "latest_runs", "latest_runs_params", "run_recorded",
]

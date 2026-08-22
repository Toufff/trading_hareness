"""Small persistence boundary for scheduled task execution evidence."""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Json


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
    bounded = max(1, min(int(limit), 100))
    rows = connection.execute(
        """SELECT run_id,task_key,run_key,cadence,as_of_date,status,methodology_version,
                      input_summary,output_summary,error_class,error_message,started_at,finished_at,updated_at
                 FROM quant.automation_runs
                WHERE (%s::text IS NULL OR task_key=%s)
                ORDER BY started_at DESC LIMIT %s""", (task_key, task_key, bounded),
    ).fetchall()
    return [dict(row) for row in rows]


def run_recorded(database: Any, *, task_key: str, run_key: str, operation: Any,
                 cadence: str | None = None, as_of_date: date | None = None,
                 methodology_version: str | None = None,
                 input_summary: dict[str, Any] | None = None) -> Any:
    """Execute one synchronous task while recording durable lifecycle state."""
    with database.transaction() as connection:
        run_id = start_run(connection, task_key=task_key, run_key=run_key, cadence=cadence,
                           as_of_date=as_of_date, methodology_version=methodology_version,
                           input_summary=input_summary)
    try:
        result = operation()
    except Exception as error:
        with database.transaction() as connection:
            fail_run(connection, run_id, error)
        raise
    with database.transaction() as connection:
        finish_run(connection, run_id, output_summary={"status": result.get("status")} if isinstance(result, dict) else {})
    return result


__all__ = ["start_run", "finish_run", "fail_run", "latest_runs", "run_recorded"]

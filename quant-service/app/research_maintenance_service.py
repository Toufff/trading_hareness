"""Local maintenance writes for research provenance and durable ledgers.

These operations intentionally do not reach market providers or change live
strategy parameters.  They are kept together because each is an explicitly
auditable database mutation exposed through a bounded API worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class ResearchMaintenanceDependencies:
    database: Any
    china_today: Callable[[], date]
    exchange_for: Callable[[str], str]
    rebuild_analyst_research: Callable[[Any, date], dict[str, Any]]
    sync_universe_membership_history: Callable[..., dict[str, Any]]
    http_exception: type[Exception]
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def update_analyst_profile(
    analyst_id: str,
    payload: Any,
    deps: ResearchMaintenanceDependencies,
) -> dict[str, Any]:
    """Store an explicit manual provenance prior without a live trade effect."""
    with deps.database.transaction() as connection:
        exists = connection.execute(
            "SELECT 1 FROM quant.remote_analysts WHERE remote_analyst_id=%s", (analyst_id,),
        ).fetchone()
        if not exists:
            raise deps.http_exception(status_code=404, detail="remote analyst not found")
        connection.execute(
            """INSERT INTO quant.analyst_research_profiles(remote_analyst_id,independence_class,audience_size,audience_as_of,evidence)
               VALUES(%s,%s,%s,%s,%s)
               ON CONFLICT(remote_analyst_id) DO UPDATE SET independence_class=EXCLUDED.independence_class,
                 audience_size=EXCLUDED.audience_size,audience_as_of=EXCLUDED.audience_as_of,evidence=EXCLUDED.evidence,updated_at=now()""",
            (analyst_id, payload.independence_class, payload.audience_size, payload.audience_as_of, payload.evidence),
        )
        result = deps.rebuild_analyst_research(connection, deps.china_today())
    return {
        "analyst_id": analyst_id, "status": "updated", "research_status": result["sleeping_experts"]["status"],
        "boundary": "manual provenance prior; no live strategy effect",
    }


def update_universe_members(payload: Any, deps: ResearchMaintenanceDependencies) -> dict[str, Any]:
    """Update explicit member flags and retain their point-in-time history."""
    with deps.database.transaction() as connection:
        for symbol in payload.symbols:
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'universe') ON CONFLICT(symbol) DO NOTHING",
                (symbol, deps.exchange_for(symbol)),
            )
            connection.execute(
                """INSERT INTO quant.universe_members(universe_key,symbol,enabled,priority,source,updated_at)
                   VALUES(%s,%s,%s,%s,'api',now()) ON CONFLICT(universe_key,symbol) DO UPDATE SET enabled=EXCLUDED.enabled,
                   priority=EXCLUDED.priority,source=EXCLUDED.source,updated_at=now()""",
                (payload.universe_key, symbol, payload.enabled, payload.priority),
            )
        active = connection.execute(
            "SELECT symbol FROM quant.universe_members WHERE universe_key=%s AND enabled ORDER BY symbol",
            (payload.universe_key,),
        ).fetchall()
        history = deps.sync_universe_membership_history(
            connection, payload.universe_key, deps.china_today(),
            [str(row["symbol"]) for row in active], source="universe-members-api", priority=payload.priority,
        )
    return {
        "universe_key": payload.universe_key, "updated": len(payload.symbols), "enabled": payload.enabled,
        "history": history,
    }


#: A run stuck ``status='running'`` past this age almost certainly belongs to
#: a process that was SIGKILLed rather than one still genuinely working: no
#: bounded database-executor call in this service runs anywhere near 2 hours.
STALE_AUTOMATION_RUN_AGE = timedelta(hours=2)


def reconcile_stale_automation_runs(deps: ResearchMaintenanceDependencies) -> dict[str, Any]:
    """Fail durable run receipts orphaned by a killed process (audit B-HIGH).

    ``automation_runs`` has no reaper: a SIGKILL between ``start_run``/
    ``start_or_resume_run`` and ``finish_run``/``fail_run`` leaves the row
    ``status='running'`` forever, which both looks like a live task in
    ``/api/v1/automation/runs`` and (via ``start_or_resume_run``'s
    ``CASE WHEN status='completed'`` guard) is *not* itself a false
    "already completed" signal, but it also never lets the scheduler's
    ``run_recorded`` stale-running skip clear so a fresh attempt has to wait
    out ``updated_at`` regardless. This always runs; unlike
    ``reconcile_stale_fetch_runs`` it takes no request payload because it is
    meant to run unconditionally on a fixed cadence, not from a manual
    operator-triggered endpoint.
    """
    cutoff = deps.now_utc() - STALE_AUTOMATION_RUN_AGE
    with deps.database.transaction() as connection:
        rows = connection.execute(
            """SELECT run_id,task_key,run_key,status,started_at,updated_at
                 FROM quant.automation_runs
                WHERE status='running' AND updated_at<%s
                ORDER BY updated_at""",
            (cutoff,),
        ).fetchall()
        if rows:
            connection.execute(
                """UPDATE quant.automation_runs
                      SET status='failed',finished_at=now(),error_class='stale_running_reconciled',
                          error_message='Run exceeded the operational max age and was reconciled by the periodic retention/maintenance task',
                          updated_at=now()
                    WHERE status='running' AND updated_at<%s""",
                (cutoff,),
            )
    return {"status": "completed", "max_age_hours": STALE_AUTOMATION_RUN_AGE.total_seconds() / 3600,
            "matched": len(rows), "items": [dict(row) for row in rows]}


def reconcile_stale_fetch_runs(payload: Any, deps: ResearchMaintenanceDependencies) -> dict[str, Any]:
    """Reconcile only stale `running` ledger rows; dry runs never mutate them."""
    cutoff = deps.now_utc() - timedelta(minutes=payload.max_age_minutes)
    with deps.database.transaction() as connection:
        rows = connection.execute(
            """SELECT fetch_run_id,provider_key,capability,trade_date,request_key,status,started_at,created_at
                 FROM quant.fetch_runs
                WHERE status='running' AND coalesce(started_at,created_at)<%s
                ORDER BY coalesce(started_at,created_at)""",
            (cutoff,),
        ).fetchall()
        if not payload.dry_run and rows:
            connection.execute(
                """UPDATE quant.fetch_runs
                      SET status=%s,finished_at=now(),error_class='stale_running_reconciled',
                          error_message='Run exceeded the operational max age and was reconciled by /operations/fetch-runs/reconcile-stale'
                    WHERE status='running' AND coalesce(started_at,created_at)<%s""",
                (payload.terminal_status, cutoff),
            )
    return {
        "status": "dry_run" if payload.dry_run else "completed", "max_age_minutes": payload.max_age_minutes,
        "terminal_status": payload.terminal_status, "matched": len(rows), "items": rows,
    }


__all__ = [
    "ResearchMaintenanceDependencies", "STALE_AUTOMATION_RUN_AGE", "reconcile_stale_automation_runs",
    "reconcile_stale_fetch_runs", "update_analyst_profile", "update_universe_members",
]

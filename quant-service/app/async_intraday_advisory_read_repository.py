"""Secret-free read projection for advisory health and recent evidence."""

from __future__ import annotations

from typing import Any

from .intraday_advisory.presentation import humanize_text


def humanize_event(row: Any) -> dict[str, Any]:
    event = dict(row)
    if event.get("summary") is not None:
        event["summary"] = humanize_text(event["summary"])
    return event


async def status(async_database: Any, *, limit: int = 20) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 100))
    async with async_database.transaction() as connection:
        result = await connection.execute("""
            SELECT runtime_key,state,account_key,last_tick_at,scope_size,last_error,details,updated_at
              FROM quant.intraday_advisory_runtime_status WHERE runtime_key='primary'""")
        runtime = await result.fetchone()
        events_result = await connection.execute("""
            SELECT event_id,symbol,name,event_kind,direction,severity,observed_at,scope_source,metrics,summary
              FROM quant.intraday_advisory_events ORDER BY observed_at DESC,created_at DESC LIMIT %s""", (bounded,))
        events = [humanize_event(row) for row in await events_result.fetchall()]
        analysis_result = await connection.execute("""
            SELECT analysis_run_id,provider,trigger_kind,report_kind,started_at,completed_at,status,
                   output->>'market_state' AS market_state,error_message
              FROM quant.intraday_advisory_analysis_runs ORDER BY completed_at DESC LIMIT %s""", (bounded,))
        analyses = [dict(row) for row in await analysis_result.fetchall()]
    return {"runtime": dict(runtime) if runtime else None, "recent_events": events, "recent_analyses": analyses}


__all__ = ["humanize_event", "status"]

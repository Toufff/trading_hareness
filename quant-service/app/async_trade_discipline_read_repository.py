"""Native async read projections for stored trade-discipline plans.

Read-only by construction: every statement here is a ``SELECT`` issued on the
``AsyncDatabase`` pool, so a dashboard refresh never consumes a bounded
blocking-executor slot.  Writes stay in the synchronous
``app/trade_discipline/repository.py``, which the CLI drives.

The column list and the default status filter are imported from that repository
rather than copied, so a stored column can never drift between the writer and
this reader.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from .trade_discipline.chart import LONGHU_MINUTE_SOURCE, PLAN_BAR_COUNT
from .trade_discipline.repository import ACTIVE_STATUSES, PLAN_COLUMNS

EVALUATION_COLUMNS = """evaluation_id,plan_id,as_of_at,trading_date,basis,line_states,plan_state,
                        inputs_hash,content_hash,created_at"""
MAX_LIMIT = 200


def _limit(value: Any, maximum: int = MAX_LIMIT) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(requested, maximum))


def valid_uuid(value: str) -> str | None:
    """``None`` for anything that is not a plan identifier, so a bad path 404s."""
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


async def latest_plans(async_database: Any, account_key: str, *,
                       statuses: tuple[str, ...] = ACTIVE_STATUSES, limit: int = 50) -> list[dict[str, Any]]:
    """The newest plan per symbol for one account, newest first."""
    async with async_database.transaction() as connection:
        result = await connection.execute(f"""
            SELECT * FROM (
              SELECT DISTINCT ON (symbol) {PLAN_COLUMNS}
                FROM quant.discipline_plans
               WHERE account_key=%s AND status = ANY(%s)
               ORDER BY symbol,as_of_at DESC) latest
            ORDER BY as_of_at DESC,symbol LIMIT %s""",
                                          (account_key, list(statuses), _limit(limit)))
        return [dict(row) for row in await result.fetchall()]


async def read_plan(async_database: Any, plan_id: str) -> dict[str, Any] | None:
    """One stored plan, or ``None`` when the identifier is unknown or malformed."""
    resolved = valid_uuid(plan_id)
    if resolved is None:
        return None
    async with async_database.transaction() as connection:
        result = await connection.execute(
            f"SELECT {PLAN_COLUMNS} FROM quant.discipline_plans WHERE plan_id=%s", (resolved,))
        row = await result.fetchone()
    return dict(row) if row else None


async def latest_evaluation(async_database: Any, plan_id: str, *, basis: str | None = None) -> dict[str, Any] | None:
    """The newest evaluation of one plan, optionally restricted to one basis."""
    resolved = valid_uuid(plan_id)
    if resolved is None:
        return None
    async with async_database.transaction() as connection:
        result = await connection.execute(f"""
            SELECT {EVALUATION_COLUMNS} FROM quant.discipline_evaluations
             WHERE plan_id=%s AND (%s::text IS NULL OR basis=%s)
             ORDER BY as_of_at DESC LIMIT 1""", (resolved, basis, basis))
        row = await result.fetchone()
    return dict(row) if row else None


async def plan_evaluations(async_database: Any, plan_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    """Every stored evaluation of one plan, oldest first (both bases)."""
    resolved = valid_uuid(plan_id)
    if resolved is None:
        return []
    async with async_database.transaction() as connection:
        result = await connection.execute(f"""
            SELECT {EVALUATION_COLUMNS} FROM quant.discipline_evaluations
             WHERE plan_id=%s ORDER BY as_of_at,created_at LIMIT %s""", (resolved, _limit(limit, 1000)))
        return [dict(row) for row in await result.fetchall()]


async def plan_history(async_database: Any, account_key: str, symbol: str, *,
                       limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
    """Every plan of one account/symbol - superseded, expired and rejected included - oldest first."""
    async with async_database.transaction() as connection:
        result = await connection.execute(f"""
            SELECT {PLAN_COLUMNS} FROM quant.discipline_plans
             WHERE account_key=%s AND symbol=%s
             ORDER BY as_of_at,created_at LIMIT %s""", (account_key, symbol, _limit(limit)))
        return [dict(row) for row in await result.fetchall()]


async def reconciliations(async_database: Any, account_key: str, *, symbol: str | None,
                          start: date, end: date, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
    """Stored reconciliation verdicts with their fills, plus every fill in the window.

    The fills are listed even when no verdict exists yet, so a chart can mark
    the real B/S points before (or without) a reconciliation run.
    """
    bounded = _limit(limit, 2000)
    async with async_database.transaction() as connection:
        records = await connection.execute("""
            SELECT c.compliance_id,c.plan_id,c.trade_record_id,c.line_kind,c.verdict,c.deviation,c.notes,
                   c.created_at,p.symbol,p.plan_key,p.plan_kind,p.trading_date AS plan_trading_date,
                   t.trade_date,t.trade_time,t.side,t.quantity,t.price,t.name
              FROM quant.discipline_compliance c
              JOIN quant.discipline_plans p ON p.plan_id=c.plan_id
              LEFT JOIN quant.broker_trade_records t ON t.record_id=c.trade_record_id
             WHERE p.account_key=%s AND (%s::text IS NULL OR p.symbol=%s)
               AND coalesce(t.trade_date,p.trading_date) BETWEEN %s AND %s
             ORDER BY coalesce(t.trade_date,p.trading_date),t.trade_time,c.created_at LIMIT %s""",
                                            (account_key, symbol, symbol, start, end, bounded))
        record_rows = [dict(row) for row in await records.fetchall()]
        trades = await connection.execute("""
            SELECT record_id,trade_date,trade_time,symbol,name,side,quantity,price,gross_amount,source
              FROM quant.broker_trade_records
             WHERE account_key=%s AND (%s::text IS NULL OR symbol=%s) AND trade_date BETWEEN %s AND %s
             ORDER BY trade_date,trade_time,record_id LIMIT %s""",
                                           (account_key, symbol, symbol, start, end, bounded))
        trade_rows = [dict(row) for row in await trades.fetchall()]
    return {"records": record_rows, "trades": trade_rows}


async def daily_bars_for_plan(async_database: Any, symbol: str, trading_date: date, last_day: date,
                              *, before_count: int = PLAN_BAR_COUNT) -> dict[str, list[dict[str, Any]]]:
    """The generator's bar window (last ``before_count`` rows through the plan day) and the rows after it."""
    columns = "trading_date,open,high,low,close,pre_close,volume,amount,is_suspended"
    async with async_database.transaction() as connection:
        before = await connection.execute(f"""
            SELECT {columns} FROM quant.canonical_bars_daily
             WHERE symbol=%s AND trading_date<=%s ORDER BY trading_date DESC LIMIT %s""",
                                          (symbol, trading_date, before_count))
        before_rows = [dict(row) for row in await before.fetchall()]
        after = await connection.execute(f"""
            SELECT {columns} FROM quant.canonical_bars_daily
             WHERE symbol=%s AND trading_date>%s AND trading_date<=%s ORDER BY trading_date LIMIT 60""",
                                         (symbol, trading_date, last_day))
        after_rows = [dict(row) for row in await after.fetchall()]
    return {"before": before_rows, "after": after_rows}


async def open_sessions(async_database: Any, start: date, end: date, *, exchange: str = "SSE") -> list[date]:
    """Open exchange sessions in ``[start, end]`` from ``quant.market_trade_calendar``."""
    async with async_database.transaction() as connection:
        result = await connection.execute("""
            SELECT calendar_date FROM quant.market_trade_calendar
             WHERE exchange=%s AND is_open AND calendar_date BETWEEN %s AND %s ORDER BY calendar_date""",
                                          (exchange, start, end))
        return [row["calendar_date"] for row in await result.fetchall()]


async def stored_minutes(async_database: Any, symbol: str, day: date) -> dict[str, Any]:
    """One session's stored Longhu minute bars; other vendors' rows are counted, never returned."""
    async with async_database.transaction() as connection:
        result = await connection.execute("""
            SELECT minute_bucket,bar_time,open,high,low,close,volume,amount,source_name,raw
              FROM quant.intraday_minute_sessions
             WHERE symbol=%s AND trading_date=%s AND source_name=%s ORDER BY bar_time LIMIT 400""",
                                          (symbol, day, LONGHU_MINUTE_SOURCE))
        rows = [dict(row) for row in await result.fetchall()]
        other = await connection.execute("""
            SELECT count(*)::int AS rows FROM quant.intraday_minute_sessions
             WHERE symbol=%s AND trading_date=%s AND source_name<>%s""", (symbol, day, LONGHU_MINUTE_SOURCE))
        other_rows = int((await other.fetchone() or {}).get("rows") or 0)
    return {"rows": rows, "source": LONGHU_MINUTE_SOURCE if rows else None, "other_source_rows": other_rows}


async def alert_status(async_database: Any, *, recent_limit: int = 20) -> dict[str, Any]:
    """Current runtime health plus bounded transition/delivery evidence."""
    async with async_database.transaction() as connection:
        status_result = await connection.execute("""
            SELECT runtime_key,account_key,state,last_started_at,last_completed_at,last_session_date,
                   eligible_plans,evaluated_plans,emitted_events,pending_deliveries,
                   last_reason,last_error,details,updated_at
              FROM quant.discipline_alert_runtime_status WHERE runtime_key='primary'""")
        status_row = await status_result.fetchone()
        event_result = await connection.execute("""
            SELECT e.event_id,e.plan_id,e.line_kind,e.from_state,e.to_state,e.observed_at,
                   e.payload->>'symbol' AS symbol,e.payload->>'name' AS name,
                   d.status AS delivery_status,d.attempt_count,d.sent_at,d.error_message
              FROM quant.discipline_alert_events e
              LEFT JOIN quant.discipline_alert_deliveries d ON d.event_id=e.event_id AND d.channel='feishu'
             ORDER BY e.observed_at DESC,e.created_at DESC LIMIT %s""", (_limit(recent_limit, 100),))
        events = [dict(row) for row in await event_result.fetchall()]
    return {"runtime": dict(status_row) if status_row else None, "recent_events": events}


def router_dependencies(async_database: Any, *, live_minutes: Any = None,
                        alert_transport_configured: Any = None) -> Any:
    """Every read projection the discipline router needs, wired to this module (composition helper)."""
    from .routers.trade_discipline import TradeDisciplineDependencies

    return TradeDisciplineDependencies(
        async_database=async_database, latest_plans=latest_plans, read_plan=read_plan,
        latest_evaluation=latest_evaluation, plan_history=plan_history, plan_evaluations=plan_evaluations,
        reconciliations=reconciliations, daily_bars=daily_bars_for_plan, open_sessions=open_sessions,
        stored_minutes=stored_minutes, live_minutes=live_minutes, alert_status=alert_status,
        alert_transport_configured=alert_transport_configured)


__all__ = ["EVALUATION_COLUMNS", "MAX_LIMIT", "alert_status", "daily_bars_for_plan", "latest_evaluation", "latest_plans",
           "open_sessions", "plan_evaluations", "plan_history", "read_plan", "reconciliations",
           "router_dependencies", "stored_minutes", "valid_uuid"]

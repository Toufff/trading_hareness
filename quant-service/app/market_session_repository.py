"""Exchange-clock plus persisted-calendar gates for provider requests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .market_rules import china_equity_session, china_futures_session
from .runtime_executors import ExecutorSaturatedError, run_database_blocking
from .tushare_providers import safe_error_detail

CN_TZ = ZoneInfo("Asia/Shanghai")

#: Shared by the sync gate here and the native-async gate in
#: ``async_market_session_repository.py`` so the two calendar readers never
#: drift into two different queries against the same single-row lookup.
SSE_CALENDAR_STATUS_SQL = "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s"


def calendar_date_for(now: datetime | None) -> date:
    """Shanghai exchange date for a UTC-or-naive instant, defaulting to now."""
    return (now or datetime.now(timezone.utc)).astimezone(CN_TZ).date()


#: Backward-compatible alias for the previous module-private name.
_calendar_date = calendar_date_for


def interpret_sse_calendar_row(row: Any) -> tuple[bool, str]:
    """Turn one persisted calendar row into the shared open/reason contract.

    The weekend short-circuit stays with each caller (so it can skip the
    query entirely); this only interprets what the database returned.
    """
    if row is None:
        return False, "SSE trade calendar has no entry for today; fail closed"
    if not row["is_open"]:
        return False, "SSE trade calendar marks today closed"
    return True, "SSE trade calendar marks today open"


def sse_calendar_status(database: Any, calendar_date: date) -> tuple[bool, str]:
    """Return persisted SSE state with a safe diagnostic reason."""
    if calendar_date.weekday() >= 5:
        return False, "SSE trade calendar treats weekends as closed"
    with database.transaction() as connection:
        row = connection.execute(SSE_CALENDAR_STATUS_SQL, (calendar_date,)).fetchone()
    return interpret_sse_calendar_row(row)


def sse_calendar_open(database: Any, calendar_date: date) -> bool:
    """Return the persisted SSE day state; weekends and gaps fail closed."""
    return sse_calendar_status(database, calendar_date)[0]


async def sse_calendar_open_async(
    database: Any,
    calendar_date: date,
    *,
    database_runner: Callable[..., Awaitable[Any]] = run_database_blocking,
) -> bool:
    """Async-safe SSE gate; local executor pressure conservatively closes it."""
    return (await sse_calendar_status_async(
        database, calendar_date, database_runner=database_runner,
    ))[0]


async def sse_calendar_status_async(
    database: Any,
    calendar_date: date,
    *,
    database_runner: Callable[..., Awaitable[Any]] = run_database_blocking,
) -> tuple[bool, str]:
    """Async-safe SSE state; gaps and local capacity pressure fail closed."""
    if calendar_date.weekday() >= 5:
        return False, "SSE trade calendar treats weekends as closed"

    def load_calendar() -> Any:
        with database.transaction() as connection:
            return connection.execute(SSE_CALENDAR_STATUS_SQL, (calendar_date,)).fetchone()

    try:
        row = await database_runner(load_calendar)
    except ExecutorSaturatedError as error:
        return False, f"local calendar capacity unavailable; fail closed: {safe_error_detail(str(error), 180)}"
    return interpret_sse_calendar_row(row)


def realtime_market_session(database: Any, api_name: str | None = None,
                            now: datetime | None = None) -> tuple[bool, str]:
    active, reason = china_futures_session(now) if api_name == "rt_fut_min" else china_equity_session(now)
    if not active:
        return active, reason
    calendar_open, calendar_reason = sse_calendar_status(database, _calendar_date(now))
    if not calendar_open:
        return False, calendar_reason
    return True, reason


async def realtime_market_session_async(database: Any, api_name: str | None = None,
                                        now: datetime | None = None, *,
                                        database_runner: Callable[..., Awaitable[Any]] = run_database_blocking) -> tuple[bool, str]:
    active, reason = china_futures_session(now) if api_name == "rt_fut_min" else china_equity_session(now)
    if not active:
        return active, reason
    exchange_date = _calendar_date(now)

    calendar_open, calendar_reason = await sse_calendar_status_async(
        database, exchange_date, database_runner=database_runner,
    )
    if not calendar_open:
        return False, calendar_reason
    return True, reason


__all__ = [
    "SSE_CALENDAR_STATUS_SQL", "calendar_date_for", "interpret_sse_calendar_row",
    "realtime_market_session", "realtime_market_session_async",
    "sse_calendar_open", "sse_calendar_open_async", "sse_calendar_status", "sse_calendar_status_async",
]

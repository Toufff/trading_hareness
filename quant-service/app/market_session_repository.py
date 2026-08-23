"""Exchange-clock plus persisted-calendar gates for provider requests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .market_rules import china_equity_session, china_futures_session
from .runtime_executors import ExecutorSaturatedError, run_database_blocking
from .tushare_providers import safe_error_detail

CN_TZ = ZoneInfo("Asia/Shanghai")


def _calendar_date(now: datetime | None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(CN_TZ).date()


def sse_calendar_status(database: Any, calendar_date: date) -> tuple[bool, str]:
    """Return persisted SSE state with a safe diagnostic reason."""
    if calendar_date.weekday() >= 5:
        return False, "SSE trade calendar treats weekends as closed"
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s",
            (calendar_date,),
        ).fetchone()
    if row is None:
        return False, "SSE trade calendar has no entry for today; fail closed"
    if not row["is_open"]:
        return False, "SSE trade calendar marks today closed"
    return True, "SSE trade calendar marks today open"


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
            return connection.execute(
                "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s",
                (calendar_date,),
            ).fetchone()

    try:
        row = await database_runner(load_calendar)
    except ExecutorSaturatedError as error:
        return False, f"local calendar capacity unavailable; fail closed: {safe_error_detail(str(error), 180)}"
    if row is None:
        return False, "SSE trade calendar has no entry for today; fail closed"
    if not row["is_open"]:
        return False, "SSE trade calendar marks today closed"
    return True, "SSE trade calendar marks today open"


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
    "realtime_market_session", "realtime_market_session_async",
    "sse_calendar_open", "sse_calendar_open_async", "sse_calendar_status", "sse_calendar_status_async",
]

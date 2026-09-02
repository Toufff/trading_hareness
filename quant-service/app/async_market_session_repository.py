"""Native-async exchange-calendar gates for production runtime loops.

The legacy repository keeps its executor-injected compatibility interface.
This module is used by the live composition root so frequent calendar checks
do not consume a blocking-executor slot.  Any local pool failure fails closed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .market_rules import china_equity_session, china_futures_session
from .market_session_repository import (
    SSE_CALENDAR_STATUS_SQL,
    calendar_date_for,
    interpret_sse_calendar_row,
)
from .tushare_providers import safe_error_detail


async def sse_calendar_status(async_database: Any, calendar_date: date) -> tuple[bool, str]:
    if calendar_date.weekday() >= 5:
        return False, "SSE trade calendar treats weekends as closed"
    try:
        async with async_database.transaction() as connection:
            result = await connection.execute(SSE_CALENDAR_STATUS_SQL, (calendar_date,))
            row = await result.fetchone()
    except Exception as error:  # noqa: BLE001 - a calendar failure must stop provider traffic
        return False, f"local calendar unavailable; fail closed: {safe_error_detail(str(error), 180)}"
    return interpret_sse_calendar_row(row)


async def sse_calendar_open(async_database: Any, calendar_date: date) -> bool:
    return (await sse_calendar_status(async_database, calendar_date))[0]


async def realtime_market_session(
    async_database: Any,
    api_name: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    active, reason = china_futures_session(now) if api_name == "rt_fut_min" else china_equity_session(now)
    if not active:
        return active, reason
    exchange_date = calendar_date_for(now)
    calendar_open, calendar_reason = await sse_calendar_status(async_database, exchange_date)
    return (True, reason) if calendar_open else (False, calendar_reason)


__all__ = ["realtime_market_session", "sse_calendar_open", "sse_calendar_status"]

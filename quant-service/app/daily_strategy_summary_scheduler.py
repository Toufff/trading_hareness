"""Durable, dependency-injected scheduler for the end-of-day research summary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any


TERMINAL_SUMMARY_STATUSES = frozenset({"sent", "disabled", "suppressed", "already_terminal", "attempts_exhausted"})


@dataclass(frozen=True)
class DailyStrategySummarySchedulerDependencies:
    calendar_open: Callable[[date], Awaitable[bool]]
    terminal_for_date: Callable[[date], Awaitable[bool]]
    run_summary: Callable[[date], Awaitable[dict[str, Any]]]
    now: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    report_error: Callable[[str], None] = print


def in_summary_window(local: datetime) -> bool:
    return time(19, 15) <= local.time() < time(19, 30)


async def daily_strategy_summary_scheduler_step(
    completed_dates: set[date], dependencies: DailyStrategySummarySchedulerDependencies,
    *, local: datetime | None = None,
) -> bool:
    """Run at most one date-scoped summary attempt with durable restart safety."""
    local = local or dependencies.now()
    exchange_date = local.date()
    if exchange_date in completed_dates or not in_summary_window(local):
        return False
    if not await dependencies.calendar_open(exchange_date):
        return False
    try:
        if await dependencies.terminal_for_date(exchange_date):
            completed_dates.add(exchange_date)
            return True
        result = await dependencies.run_summary(exchange_date)
        if str(result.get("status") or "") in TERMINAL_SUMMARY_STATUSES:
            completed_dates.add(exchange_date)
            return True
    except Exception as error:  # noqa: BLE001 - retry only inside this bounded window.
        dependencies.report_error(f"daily strategy summary failed: {str(error)[:300]}")
    return False


async def daily_strategy_summary_scheduler(dependencies: DailyStrategySummarySchedulerDependencies) -> None:
    completed_dates: set[date] = set()
    while True:
        await daily_strategy_summary_scheduler_step(completed_dates, dependencies)
        await dependencies.sleep(60)


__all__ = [
    "DailyStrategySummarySchedulerDependencies",
    "TERMINAL_SUMMARY_STATUSES",
    "daily_strategy_summary_scheduler",
    "daily_strategy_summary_scheduler_step",
    "in_summary_window",
]

"""Independent timing and retry loop for the ten-day shadow projection."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo


def post_close_materialization_window(value: datetime) -> bool:
    local = value.astimezone(ZoneInfo("Asia/Shanghai"))
    return time(18, 55) <= local.time() < time(20, 30)


@dataclass(frozen=True)
class TenDayLeaderRotationSchedulerDependencies:
    calendar_open: Callable[[date], Awaitable[bool]]
    ready_window: Callable[[datetime], bool]
    completed_for_date: Callable[[date], Awaitable[bool]]
    run: Callable[[date], Awaitable[str]]
    now: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    report_error: Callable[[str], None] = print


async def ten_day_leader_rotation_scheduler_step(
    completed_dates: set[date],
    dependencies: TenDayLeaderRotationSchedulerDependencies,
    *,
    local: datetime | None = None,
) -> bool:
    local = local or dependencies.now()
    exchange_date = local.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if exchange_date in completed_dates:
        return False
    if not dependencies.ready_window(local) or not await dependencies.calendar_open(exchange_date):
        return False
    try:
        completed = await dependencies.completed_for_date(exchange_date)
        if not completed:
            completed = await dependencies.run(exchange_date) in {"completed", "partial"}
        if completed:
            completed_dates.add(exchange_date)
            return True
    except Exception as error:  # noqa: BLE001 - bounded same-date window owns retries
        dependencies.report_error(f"ten-day leader rotation failed: {str(error)[:300]}")
    return False


async def ten_day_leader_rotation_scheduler(
    dependencies: TenDayLeaderRotationSchedulerDependencies,
) -> None:
    completed_dates: set[date] = set()
    while True:
        await ten_day_leader_rotation_scheduler_step(completed_dates, dependencies)
        await dependencies.sleep(60)


__all__ = [
    "TenDayLeaderRotationSchedulerDependencies", "post_close_materialization_window",
    "ten_day_leader_rotation_scheduler", "ten_day_leader_rotation_scheduler_step",
]

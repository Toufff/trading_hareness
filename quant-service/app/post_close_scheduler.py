"""Durable, dependency-injected post-close strategy scheduler.

The scheduler owns only time-window and same-exchange-date semantics.  Data
access, strategy execution and persistence stay behind callbacks supplied by
the composition root.  This makes the important invariant testable without a
database or provider: an older completed run can never make today's scheduler
claim completion.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class PostCloseSchedulerDependencies:
    """All side effects needed by the post-close orchestration loop."""

    calendar_open: Callable[[date], Awaitable[bool]]
    retry_window: Callable[[datetime], bool]
    completed_for_date: Callable[[date], Awaitable[tuple[bool, bool]]]
    run_strategy: Callable[[date], Awaitable[str]]
    run_main_wave: Callable[[date], Awaitable[str]]
    now: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    report_error: Callable[[str], None] = print


async def post_close_scheduler_step(
    completed_dates: set[date], dependencies: PostCloseSchedulerDependencies,
    *, local: datetime | None = None,
) -> bool:
    """Perform at most one date-scoped attempt and return whether it completed.

    ``completed_dates`` is deliberately supplied by the caller rather than
    hidden in module state.  A restart reloads truth from the same-date durable
    run tables through ``completed_for_date``; it cannot inherit a stale date
    from a previous process.
    """
    local = local or dependencies.now()
    exchange_date = local.date()
    if exchange_date in completed_dates:
        return False
    if not dependencies.retry_window(local) or not await dependencies.calendar_open(exchange_date):
        return False
    try:
        strategy_completed, main_wave_completed = await dependencies.completed_for_date(exchange_date)
        if not strategy_completed:
            strategy_completed = await dependencies.run_strategy(exchange_date) in {"completed", "partial"}
        if strategy_completed and not main_wave_completed:
            main_wave_completed = await dependencies.run_main_wave(exchange_date) == "completed"
        if strategy_completed and main_wave_completed:
            completed_dates.add(exchange_date)
            return True
    except Exception as error:  # noqa: BLE001 - bounded retry window handles transient upstream delays.
        dependencies.report_error(f"post-close strategy run failed: {str(error)[:300]}")
    return False


async def post_close_strategy_scheduler(dependencies: PostCloseSchedulerDependencies) -> None:
    """Run a bounded once-per-minute post-close scheduler forever."""
    completed_dates: set[date] = set()
    while True:
        completed = await post_close_scheduler_step(completed_dates, dependencies)
        # Preserve the previous one-minute pacing even when a date is already
        # completed.  It is inexpensive and avoids a special busy-loop path.
        await dependencies.sleep(60)
        if completed:
            continue


__all__ = [
    "PostCloseSchedulerDependencies",
    "post_close_scheduler_step",
    "post_close_strategy_scheduler",
]

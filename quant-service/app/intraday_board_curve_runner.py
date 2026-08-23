"""One-minute board-flow curve runtime scheduling without provider coupling."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo


AsyncCall = Callable[..., Awaitable[Any]]


def next_delay(local: datetime) -> float:
    """Wake near, but never before, the next observation minute boundary."""
    upcoming = (local + timedelta(minutes=1)).replace(second=1, microsecond=0)
    return min(30.0, max(1.0, (upcoming - local).total_seconds()))


async def run_iteration(
    completed_minute: datetime | None,
    pruned_on: date | None,
    *,
    board_session: AsyncCall,
    prune_before: AsyncCall,
    storage_allowed: AsyncCall,
    capture: AsyncCall,
    curve_retention_days: Callable[[], int],
    rotation_retention_days: Callable[[], int],
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    emit: Callable[[str], None] = print,
) -> tuple[datetime | None, date | None, float]:
    """Run a single board snapshot attempt; missed minutes are never replayed."""
    observed_at = now_utc()
    local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    active, _ = await board_session()
    minute = local.replace(second=0, microsecond=0)
    if active and minute != completed_minute:
        if pruned_on != local.date():
            await prune_before(observed_at, curve_retention_days(), rotation_retention_days())
            pruned_on = local.date()
        allowed, storage = await storage_allowed()
        if not allowed:
            emit(f"intraday board curve skipped by storage guard: {storage.get('state')}")
        else:
            try:
                await capture()
            except Exception as error:  # noqa: BLE001 - a later minute is independently useful
                emit(f"intraday board curve capture failed: {str(error)[:300]}")
        completed_minute = minute
    return completed_minute, pruned_on, next_delay(local)


async def run_loop(**dependencies: Any) -> None:
    completed_minute: datetime | None = None
    pruned_on: date | None = None
    while True:
        completed_minute, pruned_on, delay = await run_iteration(completed_minute, pruned_on, **dependencies)
        await asyncio.sleep(delay)


__all__ = ["next_delay", "run_iteration", "run_loop"]

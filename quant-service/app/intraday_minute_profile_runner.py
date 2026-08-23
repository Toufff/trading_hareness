"""Close-window scheduler for explicit-watch minute profile capture."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo


AsyncCall = Callable[..., Awaitable[Any]]


async def run_iteration(
    completed: set[date],
    *,
    calendar_open: AsyncCall,
    load_symbols: AsyncCall,
    storage_allowed: AsyncCall,
    capture: AsyncCall,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    emit: Callable[[str], None] = print,
) -> set[date]:
    """Attempt at most one same-day capture during the bounded close window."""
    local = now_utc().astimezone(ZoneInfo("Asia/Shanghai"))
    if local.date() in completed or not (time(14, 55) <= local.time() < time(15, 0)):
        return completed
    if not await calendar_open(local.date()):
        return completed
    symbols = await load_symbols()
    if not symbols:
        return completed
    allowed, storage = await storage_allowed()
    if not allowed:
        emit(f"intraday minute-profile capture skipped by storage guard: {storage.get('state')}")
        completed.add(local.date())
        return completed
    try:
        result = await capture(symbols)
    except Exception as error:  # noqa: BLE001 - the remaining close window is a valid retry opportunity
        emit(f"intraday minute profile capture failed: {str(error)[:300]}")
        return completed
    if result.get("status") in {"completed", "partial", "blocked"}:
        completed.add(local.date())
    return completed


async def run_loop(*, sleep_seconds: float, **dependencies: Any) -> None:
    """Run continuously; only the injected capture action may access sources."""
    completed: set[date] = set()
    while True:
        completed = await run_iteration(completed, **dependencies)
        await asyncio.sleep(sleep_seconds)


__all__ = ["run_iteration", "run_loop"]

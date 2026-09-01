"""Cadence-only runner for all-A auction/limit-pool evidence capture."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


def event_capture_window(now: datetime) -> tuple[bool, bool]:
    local = now.astimezone(CN_TZ)
    if local.weekday() >= 5:
        return False, False
    current = local.time()
    active = (time(9, 20) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(15, 0))
    return active, current >= time(14, 57)


async def run_market_event_capture_loop(
    *,
    interval_seconds: int,
    capture: Callable[..., Awaitable[dict[str, Any]]],
    session_open: Callable[[datetime], Awaitable[bool]],
    symbols: Callable[[], Awaitable[Sequence[str]]],
    log: Callable[[str], None] = print,
) -> None:
    last_auction_date: str | None = None
    while True:
        now = datetime.now(timezone.utc)
        active, auction_window = event_capture_window(now)
        if active and await session_open(now):
            local_date = now.astimezone(CN_TZ).date().isoformat()
            include_auction = auction_window and last_auction_date != local_date
            try:
                result = await capture(
                    now, include_auction=include_auction,
                    auction_symbols=await symbols() if include_auction else (),
                )
                if include_auction and result.get("auction", {}).get("received", 0) > 0:
                    last_auction_date = local_date
                if result.get("status") not in {"completed", "empty"}:
                    log(f"market event evidence capture degraded: {str(result)[:500]}")
            except Exception as error:  # noqa: BLE001 - next cadence retries
                log(f"market event evidence capture failed: {str(error)[:300]}")
        await asyncio.sleep(max(10, min(300, int(interval_seconds))))


__all__ = ["event_capture_window", "run_market_event_capture_loop"]

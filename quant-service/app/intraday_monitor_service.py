"""Dependency-injected coordinator for the durable intraday monitor loop.

The coordinator owns cadence, board-refresh scheduling and successful-scan
rotation advancement.  It deliberately knows nothing about FastAPI, database
connections or strategy rules, keeping the live control flow testable and
separate from the application's HTTP surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


def next_rotation_offset_from_scan(scan_result: Any, current_offset: int, *, maximum: int = 40) -> int:
    """Advance only after a completed scan returned an audited cursor.

    A failed or malformed scan keeps the same minute-validation slice for the
    next pass.  This makes provider errors retryable instead of silently
    creating a coverage hole.
    """
    fallback = max(0, int(current_offset))
    if not isinstance(scan_result, dict):
        return fallback
    validation = scan_result.get("realtime_validation")
    next_offset = validation.get("next_offset") if isinstance(validation, dict) else None
    if isinstance(next_offset, int) and not isinstance(next_offset, bool) and 0 <= next_offset <= maximum:
        return next_offset
    return fallback


async def run_intraday_monitor_loop(
    interval_seconds: int,
    *,
    realtime_session: Callable[[], Awaitable[tuple[bool, str]]],
    high_frequency_window: Callable[[datetime], bool],
    next_delay_seconds: Callable[[int, datetime], float],
    make_scan_request: Callable[[int, int], Any],
    scan_watchlist: Callable[[Any], Awaitable[dict[str, Any]]],
    board_refresh_interval_seconds: Callable[[datetime], float],
    run_board_report: Callable[..., Awaitable[dict[str, Any]]],
    log: Callable[[str], None] = print,
) -> None:
    """Run one durable monitor owner; callers provide all market operations."""
    loop = asyncio.get_running_loop()
    next_started_at = loop.time()
    next_board_refresh_at = loop.time()
    realtime_rotation_offset = 0
    while True:
        await asyncio.sleep(max(0.0, next_started_at - loop.time()))
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        # A slow provider pass must never generate a stale catch-up burst.
        next_started_at = loop.time() + next_delay_seconds(interval_seconds, local)
        try:
            active, _ = await realtime_session()
            if not active:
                continue
            minute_limit = 0 if high_frequency_window(local) else 4
            jobs = [scan_watchlist(make_scan_request(minute_limit, realtime_rotation_offset))]
            if loop.time() >= next_board_refresh_at:
                next_board_refresh_at = loop.time() + board_refresh_interval_seconds(local)
                # Board reports are frontend research evidence; watched-stock
                # signals remain the only path to Feishu.
                jobs.append(run_board_report(deliver=False))
            results = await asyncio.gather(*jobs, return_exceptions=True)
            realtime_rotation_offset = next_rotation_offset_from_scan(
                results[0] if results else None,
                realtime_rotation_offset,
            )
            for result in results:
                if isinstance(result, Exception):
                    log(f"intraday monitor source pass failed: {str(result)[:300]}")
        except Exception as error:  # noqa: BLE001 - a later interval may recover a public source
            log(f"intraday monitor iteration failed: {str(error)[:300]}")


__all__ = ["next_rotation_offset_from_scan", "run_intraday_monitor_loop"]

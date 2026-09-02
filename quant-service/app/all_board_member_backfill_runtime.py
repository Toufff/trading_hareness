"""Clock loop for durable all-board member coverage backfill.

The batch use case itself (``all_board_member_backfill_service.run``) already
owns its business logic and per-board durable state; this module owns only
the polling cadence (quieter post-close window, fixed retry delay) so the
ASGI composition root does not carry a bare ``while True`` loop body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class AllBoardMemberBackfillLoopDependencies:
    sse_calendar_open_async: Callable[[date], Awaitable[bool]]
    run_batch: Callable[[], Awaitable[object]]
    log_failure: Callable[[str], None]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


async def all_board_member_backfill_loop(dependencies: AllBoardMemberBackfillLoopDependencies) -> None:
    """Use the quieter post-close window for durable all-board coverage."""
    while True:
        local = dependencies.now().astimezone(ZoneInfo("Asia/Shanghai"))
        if await dependencies.sse_calendar_open_async(local.date()) and time(15, 10) <= local.time() < time(18, 0):
            try:
                await dependencies.run_batch()
            except Exception as error:  # noqa: BLE001 - durable per-board states make the next batch safe
                dependencies.log_failure(str(error))
            await dependencies.sleep(90)
            continue
        await dependencies.sleep(60)


__all__ = ["AllBoardMemberBackfillLoopDependencies", "all_board_member_backfill_loop"]

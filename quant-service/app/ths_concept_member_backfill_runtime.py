"""Clock loop for the post-close THS concept member backfill.

Mirrors ``all_board_member_backfill_runtime``: the batch use case
(``ths_concept_member_backfill_service.run``) owns the fail-closed member
sync itself; this module owns only the polling cadence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ThsConceptMemberBackfillLoopDependencies:
    sse_calendar_open_async: Callable[[date], Awaitable[bool]]
    run_batch: Callable[[], Awaitable[object]]
    log_failure: Callable[[str], None]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


async def ths_concept_member_backfill_loop(dependencies: ThsConceptMemberBackfillLoopDependencies) -> None:
    """After close, complete one rate-bounded THS member batch at a time."""
    while True:
        local = dependencies.now().astimezone(ZoneInfo("Asia/Shanghai"))
        if await dependencies.sse_calendar_open_async(local.date()) and time(15, 10) <= local.time() < time(18, 0):
            try:
                await dependencies.run_batch()
            except Exception as error:  # noqa: BLE001 - durable state makes the next batch safe to retry
                dependencies.log_failure(str(error))
            await dependencies.sleep(65)
            continue
        await dependencies.sleep(60)


__all__ = ["ThsConceptMemberBackfillLoopDependencies", "ths_concept_member_backfill_loop"]

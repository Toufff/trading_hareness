"""Production adapter between the independent scheduler and materializer."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from .ten_day_leader_rotation_scheduler import TenDayLeaderRotationSchedulerDependencies


@dataclass(frozen=True)
class TenDayLeaderRotationRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    calendar_open: Callable[[date], Awaitable[bool]]
    persisted_completed_for_date: Callable[..., bool]
    run_materialization: Callable[[Any], dict[str, Any]]
    request: Callable[..., Any]
    model_version: str
    ready_window: Callable[[datetime], bool]
    now: Callable[[], datetime]
    scheduler: Callable[[TenDayLeaderRotationSchedulerDependencies], Awaitable[None]]


async def run_ten_day_leader_rotation_loop(
    dependencies: TenDayLeaderRotationRuntimeDependencies,
) -> None:
    async def completed_for_date(exchange_date: date) -> bool:
        return bool(await dependencies.run_database(
            functools.partial(
                dependencies.persisted_completed_for_date,
                dependencies.database,
                exchange_date,
                model_version=dependencies.model_version,
            ),
            timeout_seconds=10,
        ))

    async def run(exchange_date: date) -> str:
        result = await dependencies.run_database(
            dependencies.run_materialization,
            dependencies.request(as_of_date=exchange_date),
            timeout_seconds=90,
        )
        return str(result.get("status") or "failed")

    await dependencies.scheduler(TenDayLeaderRotationSchedulerDependencies(
        calendar_open=dependencies.calendar_open,
        ready_window=dependencies.ready_window,
        completed_for_date=completed_for_date,
        run=run,
        now=dependencies.now,
    ))


__all__ = ["TenDayLeaderRotationRuntimeDependencies", "run_ten_day_leader_rotation_loop"]

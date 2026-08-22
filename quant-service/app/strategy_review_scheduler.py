"""Dependency-injected midday/close strategy-review scheduler.

This module owns only checkpoint timing and completion bookkeeping.  The
composition root supplies every database, provider and report operation so
the scheduler is deterministic and unit-testable without live market access.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any


REVIEW_CHECKPOINTS: tuple[tuple[str, time], ...] = (("midday", time(11, 31)), ("close", time(15, 5)))


@dataclass(frozen=True)
class StrategyReviewSchedulerDependencies:
    calendar_open: Callable[[date], Awaitable[bool]]
    sync_index_context: Callable[[date], Awaitable[Any]]
    build_market_snapshot: Callable[[date, str], Awaitable[Any]]
    build_board_report: Callable[[], Awaitable[Any]]
    recompute_outcomes: Callable[[date], Awaitable[Any]]
    recompute_analyst_intraday_outcomes: Callable[[date], Awaitable[Any]]
    recompute_scorecards: Callable[[date], Awaitable[Any]]
    persist_review: Callable[[date, str], Awaitable[Any]]
    now: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    report_error: Callable[[str], None] = print
    build_analyst_market_review: Callable[[str, date], Awaitable[Any]] | None = None


async def strategy_review_scheduler_step(
    completed: set[tuple[date, str]], dependencies: StrategyReviewSchedulerDependencies,
    *, local: datetime | None = None,
) -> tuple[str, ...]:
    """Run eligible checkpoint(s) once and return completed session names.

    The two-minute window is retained exactly.  If a callback fails, its key is
    not added, so the next fifteen-second turn can retry while still inside the
    same bounded window.
    """
    local = local or dependencies.now()
    exchange_date = local.date()
    if not await dependencies.calendar_open(exchange_date):
        return ()
    completed_now: list[str] = []
    for session, checkpoint in REVIEW_CHECKPOINTS:
        key = (exchange_date, session)
        checkpoint_end = (datetime.combine(exchange_date, checkpoint) + timedelta(minutes=2)).time()
        if key in completed or not (checkpoint <= local.time() < checkpoint_end):
            continue
        try:
            if session == "close":
                await dependencies.sync_index_context(exchange_date)
            await dependencies.build_market_snapshot(exchange_date, session)
            await dependencies.build_board_report()
            if session == "close":
                # Settlement reads persisted data only; it cannot add a live
                # provider response to an already-recorded checkpoint.
                await dependencies.recompute_outcomes(exchange_date)
                # Analyst action outcomes are a separate, research-only ledger;
                # settle them at the same close checkpoint so the next review
                # sees both daily and bounded 5/15/30/60-minute paths.
                await dependencies.recompute_analyst_intraday_outcomes(exchange_date)
                await dependencies.recompute_scorecards(exchange_date)
                if dependencies.build_analyst_market_review is not None:
                    await dependencies.build_analyst_market_review("daily", exchange_date)
                    if exchange_date.weekday() == 4:
                        await dependencies.build_analyst_market_review("weekly", exchange_date)
            await dependencies.persist_review(exchange_date, session)
            completed.add(key)
            completed_now.append(session)
        except Exception as error:  # noqa: BLE001 - retry only within checkpoint window.
            dependencies.report_error(f"strategy review checkpoint {session} failed: {str(error)[:300]}")
    return tuple(completed_now)


async def strategy_review_scheduler(dependencies: StrategyReviewSchedulerDependencies) -> None:
    """Run the point-in-time midday/close review scheduler forever."""
    completed: set[tuple[date, str]] = set()
    while True:
        await strategy_review_scheduler_step(completed, dependencies)
        await dependencies.sleep(15)


__all__ = [
    "REVIEW_CHECKPOINTS",
    "StrategyReviewSchedulerDependencies",
    "strategy_review_scheduler_step",
    "strategy_review_scheduler",
]

"""Production adapters for scheduled review and post-close strategy work.

Schedulers own timing and retry semantics; this module owns the small amount
of dependency composition needed to connect them to the application's bounded
database executor.  It contains no provider client, strategy threshold or
FastAPI ownership.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from .post_close_scheduler import PostCloseSchedulerDependencies
from .strategy_review_scheduler import StrategyReviewSchedulerDependencies


@dataclass(frozen=True)
class StrategyReviewRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    calendar_open: Callable[[date], Awaitable[bool]]
    sync_index_context: Callable[[date], Awaitable[Any]]
    build_market_snapshot: Callable[[Any], Awaitable[dict[str, Any]]]
    market_snapshot_request: Callable[[str], Any]
    build_board_report: Callable[..., Awaitable[dict[str, Any]]]
    recompute_outcomes: Callable[[date | None], dict[str, Any]]
    recompute_analyst_intraday_outcomes: Callable[[date], dict[str, Any]]
    recompute_scorecards: Callable[[date | None], dict[str, Any]]
    strategy_review_payload: Callable[[Any, Any], dict[str, Any]]
    strategy_review_request: Callable[..., Any]
    completed_for_checkpoint: Callable[[Any, date, str], bool]
    build_analyst_market_review: Callable[..., dict[str, Any]]
    now: Callable[[], datetime]
    scheduler: Callable[[StrategyReviewSchedulerDependencies], Awaitable[None]]


async def run_strategy_review_loop(dependencies: StrategyReviewRuntimeDependencies) -> None:
    """Run existing review checkpoints with their original local boundaries."""
    async def build_snapshot(exchange_date: date, session: str) -> dict[str, Any]:
        # The snapshot request intentionally has no historical date override:
        # the scheduler reaches this callback only at its current checkpoint.
        _ = exchange_date
        return await dependencies.build_market_snapshot(dependencies.market_snapshot_request(session))

    async def build_board_report() -> dict[str, Any]:
        return await dependencies.build_board_report(deliver=False)

    async def settle_outcomes(exchange_date: date) -> dict[str, Any]:
        return await dependencies.run_database(dependencies.recompute_outcomes, exchange_date, timeout_seconds=60)

    async def settle_analyst_intraday_outcomes(exchange_date: date) -> dict[str, Any]:
        return await dependencies.run_database(
            dependencies.recompute_analyst_intraday_outcomes, exchange_date, timeout_seconds=90,
        )

    async def settle_scorecards(exchange_date: date) -> dict[str, Any]:
        return await dependencies.run_database(dependencies.recompute_scorecards, exchange_date, timeout_seconds=30)

    async def persist_review(exchange_date: date, session: str) -> None:
        def persist() -> None:
            with dependencies.database.transaction() as connection:
                dependencies.strategy_review_payload(
                    connection, dependencies.strategy_review_request(session=session, as_of_date=exchange_date, persist=True),
                )
        await dependencies.run_database(persist, timeout_seconds=30)

    async def review_completed_for_checkpoint(exchange_date: date, session: str) -> bool:
        def load() -> bool:
            with dependencies.database.transaction() as connection:
                return dependencies.completed_for_checkpoint(connection, exchange_date, session)
        return bool(await dependencies.run_database(load, timeout_seconds=10))

    async def build_analyst_review(cadence: str, exchange_date: date) -> dict[str, Any]:
        return await dependencies.run_database(
            dependencies.build_analyst_market_review, dependencies.database, cadence, exchange_date, timeout_seconds=90,
        )

    await dependencies.scheduler(StrategyReviewSchedulerDependencies(
        calendar_open=dependencies.calendar_open,
        sync_index_context=dependencies.sync_index_context,
        build_market_snapshot=build_snapshot,
        build_board_report=build_board_report,
        recompute_outcomes=settle_outcomes,
        recompute_analyst_intraday_outcomes=settle_analyst_intraday_outcomes,
        recompute_scorecards=settle_scorecards,
        build_analyst_market_review=build_analyst_review,
        persist_review=persist_review,
        completed_for_checkpoint=review_completed_for_checkpoint,
        now=dependencies.now,
    ))


@dataclass(frozen=True)
class PostCloseStrategyRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    calendar_open: Callable[[date], Awaitable[bool]]
    retry_window: Callable[[datetime], bool]
    strategy_completed_for_date: Callable[[date], bool]
    main_wave_completed_for_date: Callable[[date], bool]
    run_recorded: Callable[..., dict[str, Any]]
    run_post_close_strategy: Callable[[Any], dict[str, Any]]
    post_close_request: Callable[..., Any]
    post_close_model_version: str
    run_main_wave_research: Callable[[Any], dict[str, Any]]
    main_wave_request: Callable[..., Any]
    now: Callable[[], datetime]
    scheduler: Callable[[PostCloseSchedulerDependencies], Awaitable[None]]


async def run_post_close_strategy_loop(dependencies: PostCloseStrategyRuntimeDependencies) -> None:
    """Run durable same-date post-close operations through the existing scheduler."""
    async def completed_for_date(exchange_date: date) -> tuple[bool, bool]:
        return (
            bool(await dependencies.run_database(dependencies.strategy_completed_for_date, exchange_date, timeout_seconds=10)),
            bool(await dependencies.run_database(dependencies.main_wave_completed_for_date, exchange_date, timeout_seconds=10)),
        )

    async def run_strategy(exchange_date: date) -> str:
        result = await dependencies.run_database(functools.partial(
            dependencies.run_recorded, dependencies.database, task_key="post_close_strategy",
            run_key=f"post-close-strategy:{exchange_date}",
            operation=functools.partial(dependencies.run_post_close_strategy, dependencies.post_close_request(as_of_date=exchange_date)),
            cadence="daily", as_of_date=exchange_date, methodology_version=dependencies.post_close_model_version,
            input_summary={"data_boundary": "same_date_close"},
        ), timeout_seconds=60)
        return str(result.get("status") or "failed")

    async def run_main_wave(exchange_date: date) -> str:
        result = await dependencies.run_database(functools.partial(
            dependencies.run_recorded, dependencies.database, task_key="watchlist_main_wave",
            run_key=f"watchlist-main-wave:{exchange_date}",
            operation=functools.partial(dependencies.run_main_wave_research, dependencies.main_wave_request(as_of_date=exchange_date)),
            cadence="daily", as_of_date=exchange_date, methodology_version="watchlist-main-wave-v2",
            input_summary={"universe": "watchlist"},
        ), timeout_seconds=90)
        return str(result.get("status") or "failed")

    await dependencies.scheduler(PostCloseSchedulerDependencies(
        calendar_open=dependencies.calendar_open,
        retry_window=dependencies.retry_window,
        completed_for_date=completed_for_date,
        run_strategy=run_strategy,
        run_main_wave=run_main_wave,
        now=dependencies.now,
    ))


__all__ = [
    "PostCloseStrategyRuntimeDependencies",
    "StrategyReviewRuntimeDependencies",
    "run_post_close_strategy_loop",
    "run_strategy_review_loop",
]

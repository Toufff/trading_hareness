"""Application adapter for the bounded Super GET fast-quote loop.

The loop engine owns rotation and task lifecycle.  This adapter owns only the
local watchlist read and short raw-evidence retention query, both submitted to
the existing bounded database executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from .intraday_fast_quote_service import bounded_rotation_pool_size


@dataclass(frozen=True)
class IntradayFastQuoteRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    max_symbols: Callable[[], int]
    watch_priority_key: Callable[[dict[str, Any]], Any]
    realtime_session: Callable[[], Awaitable[tuple[bool, str]]]
    high_frequency_window: Callable[[datetime], bool]
    storage_allowed: Callable[[], Awaitable[tuple[bool, dict[str, Any]]]]
    capture_quote: Callable[[str], Awaitable[dict[str, Any]]]
    observe_completed: Callable[..., None]
    interval_seconds: Callable[[], float]
    max_in_flight: Callable[[], int]
    retention_days: Callable[[], int]
    run_loop: Callable[..., Awaitable[None]]
    freshness_budget_seconds: Callable[[], float | None] = lambda: None


async def run_intraday_fast_quote_runtime_loop(
    dependencies: IntradayFastQuoteRuntimeDependencies,
) -> None:
    """Run one-second cross-checks without broadening provider access."""
    async def load_symbols() -> list[str]:
        # Rotation starts exactly one new symbol per interval tick, so more
        # configured symbols than the declared freshness budget allows would
        # silently rotate slower than the runtime-task contract promises.
        pool_size = bounded_rotation_pool_size(
            dependencies.max_symbols(), dependencies.interval_seconds(), dependencies.freshness_budget_seconds(),
        )

        def load_watches() -> list[Any]:
            with dependencies.database.transaction() as connection:
                return connection.execute(
                    "SELECT * FROM quant.intraday_watchlists WHERE enabled "
                    "ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
                    (pool_size,),
                ).fetchall()
        rows = await dependencies.run_database(load_watches)
        return [
            str(row["symbol"])
            for row in sorted((dict(row) for row in rows), key=dependencies.watch_priority_key)
        ]

    async def prune_before(cutoff: datetime) -> None:
        def prune() -> None:
            with dependencies.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM quant.intraday_quote_observations "
                    "WHERE source_name='tushare_super_get_rt_k' AND observed_at<%s",
                    (cutoff,),
                )
        await dependencies.run_database(prune)

    await dependencies.run_loop(
        realtime_session=dependencies.realtime_session,
        high_frequency_window=dependencies.high_frequency_window,
        load_symbols=load_symbols,
        prune_before=prune_before,
        storage_allowed=dependencies.storage_allowed,
        capture_quote=dependencies.capture_quote,
        observe_completed=dependencies.observe_completed,
        interval_seconds=dependencies.interval_seconds,
        max_in_flight=dependencies.max_in_flight,
        retention_days=dependencies.retention_days,
    )


__all__ = ["IntradayFastQuoteRuntimeDependencies", "run_intraday_fast_quote_runtime_loop"]

"""Application adapter for bounded Tencent order-book observation.

The loop engine keeps exchange-session, capability and cadence policy.  This
adapter owns only bounded local watchlist reads and the source-scoped evidence
retention query through the existing database executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class IntradayOrderBookRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    max_symbols: Callable[[], int]
    realtime_session: Callable[[], Awaitable[tuple[bool, str]]]
    open_capabilities: Callable[..., Awaitable[set[str]]]
    storage_allowed: Callable[[], Awaitable[tuple[bool, dict[str, Any]]]]
    capture: Callable[[list[str]], Awaitable[dict[str, Any]]]
    interval_seconds: Callable[[], float]
    retention_days: Callable[[], int]
    run_loop: Callable[..., Awaitable[None]]


async def run_intraday_order_book_runtime_loop(
    dependencies: IntradayOrderBookRuntimeDependencies,
) -> None:
    """Run depth observation without direct sync database work in main."""
    async def load_symbols() -> list[str]:
        def load_watches() -> list[Any]:
            with dependencies.database.transaction() as connection:
                return connection.execute(
                    "SELECT symbol FROM quant.intraday_watchlists WHERE enabled "
                    "ORDER BY updated_at DESC,symbol LIMIT %s",
                    (dependencies.max_symbols(),),
                ).fetchall()

        rows = await dependencies.run_database(load_watches)
        return [str(row["symbol"]) for row in rows]

    async def prune_before(now: datetime, retention_days: int) -> None:
        cutoff = now - timedelta(days=retention_days)

        def prune() -> None:
            with dependencies.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM quant.intraday_quote_observations "
                    "WHERE source_name='tencent_order_book' AND observed_at<%s",
                    (cutoff,),
                )

        await dependencies.run_database(prune)

    await dependencies.run_loop(
        realtime_session=dependencies.realtime_session,
        open_capabilities=dependencies.open_capabilities,
        load_symbols=load_symbols,
        prune_before=prune_before,
        storage_allowed=dependencies.storage_allowed,
        capture=dependencies.capture,
        interval_seconds=dependencies.interval_seconds,
        retention_days=dependencies.retention_days,
    )


__all__ = ["IntradayOrderBookRuntimeDependencies", "run_intraday_order_book_runtime_loop"]

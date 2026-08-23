"""Application adapter for bounded close-window minute-profile capture.

The scheduler owns the exchange-time window and retry semantics.  This adapter
owns only the local, bounded watchlist query submitted through the existing
database executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class IntradayMinuteProfileRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    max_symbols: Callable[[], int]
    watch_priority_key: Callable[[dict[str, Any]], Any]
    calendar_open: Callable[..., Awaitable[bool]]
    storage_allowed: Callable[[], Awaitable[tuple[bool, dict[str, Any]]]]
    capture: Callable[[list[str]], Awaitable[dict[str, Any]]]
    run_loop: Callable[..., Awaitable[None]]


async def run_intraday_minute_profile_runtime_loop(
    dependencies: IntradayMinuteProfileRuntimeDependencies,
) -> None:
    """Run close capture without moving synchronous database work to the loop."""
    async def load_symbols() -> list[str]:
        def load_watches() -> list[Any]:
            with dependencies.database.transaction() as connection:
                return connection.execute(
                    "SELECT * FROM quant.intraday_watchlists WHERE enabled "
                    "ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
                    (dependencies.max_symbols(),),
                ).fetchall()

        rows = await dependencies.run_database(load_watches)
        return [
            str(row["symbol"])
            for row in sorted((dict(row) for row in rows), key=dependencies.watch_priority_key)
        ]

    await dependencies.run_loop(
        sleep_seconds=30,
        calendar_open=dependencies.calendar_open,
        load_symbols=load_symbols,
        storage_allowed=dependencies.storage_allowed,
        capture=dependencies.capture,
    )


__all__ = [
    "IntradayMinuteProfileRuntimeDependencies",
    "run_intraday_minute_profile_runtime_loop",
]

"""Composition helper for durable user-tracking research refreshes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from .request_models import StockWorkbenchRequest
from .user_tracking_refresh import persist_error, persist_snapshot, refresh


def build_refresher(
    *,
    async_database: Any,
    database: Any,
    read_rows: Callable[[Any], Awaitable[dict[str, Any]]],
    build_workbench: Callable[[str, StockWorkbenchRequest], Awaitable[dict[str, Any]]],
    run_database: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Wire I/O at composition time while keeping the refresh service isolated."""

    async def refresh_user_tracking(
        as_of_date: date | None = None, symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        tracked = await read_rows(async_database)

        async def build_one(symbol: str, day: date | None) -> dict[str, Any]:
            return await build_workbench(
                symbol, StockWorkbenchRequest(as_of_date=day, lookback_days=120),
            )

        async def save_one(symbol: str, snapshot: dict[str, Any]) -> None:
            await run_database(persist_snapshot, database, symbol, snapshot, timeout_seconds=10)

        async def save_error(symbol: str, detail: str) -> None:
            await run_database(persist_error, database, symbol, detail, timeout_seconds=10)

        return await refresh(
            tracked.get("items") or [], as_of_date=as_of_date, build_workbench=build_one,
            save_snapshot=save_one, save_error=save_error, symbols=symbols,
        )

    return refresh_user_tracking


__all__ = ["build_refresher"]

"""Bounded close-index context with a persisted public fallback."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Awaitable, Callable


async def sync_index_context(
    as_of_date: date,
    symbols: tuple[str, ...],
    *,
    prefer_public: bool,
    primary_request: Callable[[str, date, date], Any],
    fetch_primary: Callable[[Any], Awaitable[Any]],
    fetch_public: Callable[[str, str, str], Awaitable[list[dict[str, Any]]]],
    persist_public: Callable[[str, list[dict[str, Any]]], int],
    run_database: Callable[..., Awaitable[Any]],
    fetch_secondary: Callable[[str, str, str], Awaitable[list[dict[str, Any]]]] | None = None,
    secondary_provider: str = "tencent_index_free",
) -> dict[str, Any]:
    """Persist every requested index, falling back symbol by symbol."""
    start_date = as_of_date - timedelta(days=45)
    start, end = start_date.strftime("%Y%m%d"), as_of_date.strftime("%Y%m%d")

    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        if not prefer_public:
            try:
                result = await fetch_primary(primary_request(symbol, start_date, as_of_date))
                return symbol, {"status": "completed", "provider": "tushare_primary", "result": result}
            except Exception as error:  # noqa: BLE001 - explicit labelled fallback
                primary_error = str(error)[:240]
        else:
            primary_error = "primary route bypassed because Longhu close is authoritative"
        try:
            rows = await fetch_public(symbol, start, end)
            stored = await run_database(persist_public, "eastmoney_free", rows, timeout_seconds=60)
            if not rows or stored <= 0:
                raise ValueError("public index source returned no persistable rows")
            return symbol, {
                "status": "completed", "provider": "eastmoney_free",
                "received": len(rows), "stored": stored, "primary_error": primary_error,
            }
        except Exception as error:  # noqa: BLE001 - bounded secondary fallback below
            public_error = str(error)[:240]
        if fetch_secondary is not None:
            try:
                rows = await fetch_secondary(symbol, start, end)
                stored = await run_database(persist_public, secondary_provider, rows, timeout_seconds=60)
                if not rows or stored <= 0:
                    raise ValueError("secondary public index source returned no persistable rows")
                return symbol, {
                    "status": "completed", "provider": secondary_provider,
                    "received": len(rows), "stored": stored,
                    "primary_error": primary_error, "eastmoney_error": public_error,
                }
            except Exception as secondary_error:  # noqa: BLE001 - returned as bounded source status
                return symbol, {
                    "status": "failed", "provider": secondary_provider,
                    "error": str(secondary_error)[:240], "eastmoney_error": public_error,
                    "primary_error": primary_error,
                }
        else:
            return symbol, {
                "status": "failed", "provider": "eastmoney_free",
                "error": public_error, "primary_error": primary_error,
            }

    outcomes = dict(await asyncio.gather(*(one(symbol) for symbol in symbols)))
    completed = [symbol for symbol, item in outcomes.items() if item["status"] == "completed"]
    return {
        "status": "completed" if len(completed) == len(symbols) else "partial",
        "completed": completed,
        "symbols": outcomes,
        "source": "per-symbol persisted index close with labelled fallback",
    }


__all__ = ["sync_index_context"]

"""Async read/mapping boundary for persisted fast-quote confirmation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any


async def latest_confirmations(
    symbols: list[str], quotes: dict[str, dict[str, Any]], observed_at: datetime,
    *, read_latest: Callable[[list[str]], Awaitable[list[dict[str, Any]]]],
    confirm: Callable[[dict[str, Any] | None, dict[str, Any] | None, datetime], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    latest = {str(row["symbol"]): dict(row) for row in await read_latest(symbols)}
    return {symbol: confirm(quotes.get(symbol), latest.get(symbol), observed_at) for symbol in symbols}


__all__ = ["latest_confirmations"]

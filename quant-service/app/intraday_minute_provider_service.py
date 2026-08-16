"""Bounded Super GET minute-context collection for the intraday scanner.

The module owns only response normalization and concurrent fan-out.  HTTP,
provider selection and persistence remain injected by the caller so the same
deterministic transformation is usable in tests and later event replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


MinuteRowsFetcher = Callable[[str], Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]]
MinuteFeatureBuilder = Callable[..., dict[str, Any] | None]
Number = Callable[[Any], float | None]


def normalize_super_get_minute_rows(rows: list[dict[str, Any]], *, number: Number) -> list[dict[str, Any]]:
    """Return chronological, valid minute bars with causal cumulative VWAP.

    Provider rows are retained as evidence.  Invalid price, volume and amount
    values are excluded rather than turned into zeroes, because zeroes would
    create a fictitious VWAP/volume burst for a live signal.
    """
    ordered = sorted(rows, key=lambda row: str(row.get("time") or row.get("updated_at") or ""))
    cumulative_volume, cumulative_amount = 0.0, 0.0
    normalized: list[dict[str, Any]] = []
    for row in ordered:
        volume = number(row.get("vol"))
        amount = number(row.get("amount"))
        close = number(row.get("close"))
        if volume is None or volume < 0 or amount is None or amount < 0 or close is None or close <= 0:
            continue
        cumulative_volume += volume
        cumulative_amount += amount
        normalized.append({
            **row,
            "volume_lot": volume,
            "amount": amount,
            "vwap": cumulative_amount / cumulative_volume if cumulative_volume else None,
            "is_complete": True,
        })
    return normalized


async def fetch_bounded_minute_context(
    symbols: list[str], *, fetch_rows: MinuteRowsFetcher,
    feature_builder: MinuteFeatureBuilder, number: Number,
) -> dict[str, dict[str, Any]]:
    """Fetch bounded per-symbol minute context without failing the scan.

    A failed enrichment is deliberately represented by absence of that symbol
    rather than by an exception that aborts the watchlist's primary quote
    scan.  A valid empty response retains its provider status for health and
    evidence inspection.
    """
    async def fetch_one(symbol: str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        source, rows = await fetch_rows(symbol)
        normalized = normalize_super_get_minute_rows(rows, number=number)
        feature = feature_builder(normalized, source="tushare_super_get_rt_min") if normalized else None
        payload = {"latest": normalized[-1], "feature": feature} if normalized else None
        return symbol, payload, source

    results = await asyncio.gather(*(fetch_one(symbol) for symbol in symbols), return_exceptions=True)
    output: dict[str, dict[str, Any]] = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        symbol, payload, source = result
        output[symbol] = {**(payload or {}), "source": source}
    return output


__all__ = ["fetch_bounded_minute_context", "normalize_super_get_minute_rows"]

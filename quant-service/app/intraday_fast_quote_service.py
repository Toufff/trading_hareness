"""Dependency-injected lifecycle for the optional one-second Super GET check."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def cross_source_confirmation(
    quote: dict[str, Any] | None,
    fast_quote: dict[str, Any] | None,
    observed_at: datetime,
    max_age_seconds: float = 30.0,
    *,
    number: Callable[[Any], float | None],
) -> dict[str, Any]:
    """Compare a Tencent watch price with a persisted Super GET sample.

    Provider fetches and database lookups remain outside this pure boundary.
    Live scanning and recorded-event replay can therefore share the exact
    freshness and disagreement rule without treating a missing cross-check as
    a substitute quote or a fabricated veto.
    """
    if not fast_quote:
        return {"status": "missing", "max_age_seconds": max_age_seconds}
    fast_observed_at = fast_quote.get("observed_at")
    if not isinstance(fast_observed_at, datetime):
        return {"status": "invalid", "max_age_seconds": max_age_seconds}
    age_seconds = max(0.0, (observed_at - fast_observed_at).total_seconds())
    fast_price = number(fast_quote.get("price"))
    tencent_price = number((quote or {}).get("price"))
    base = {
        "observed_at": fast_observed_at.isoformat(),
        "age_seconds": round(age_seconds, 2),
        "max_age_seconds": max_age_seconds,
        "super_get_price": fast_price,
        "tencent_price": tencent_price,
    }
    if age_seconds > max_age_seconds:
        return {**base, "status": "stale"}
    if fast_price is None or fast_price <= 0 or tencent_price is None or tencent_price <= 0:
        return {**base, "status": "invalid"}
    gap_pct = ((fast_price / tencent_price) - 1) * 100
    return {
        **base,
        "status": "confirmed" if abs(gap_pct) <= 0.8 else "mismatch",
        "gap_pct": round(gap_pct, 4),
        "allowed_gap_pct": 0.8,
    }


def fast_quote_rotation_slot(symbols: list[str], cursor: int) -> tuple[str | None, int]:
    """Choose one fair rotation item without assuming a fixed basket size."""
    if not symbols:
        return None, max(0, int(cursor))
    normalized_cursor = max(0, int(cursor))
    return symbols[normalized_cursor % len(symbols)], normalized_cursor + 1


async def run_intraday_fast_quote_loop(
    *,
    realtime_session: Callable[[], Awaitable[tuple[bool, str]]],
    high_frequency_window: Callable[[datetime], bool],
    load_symbols: Callable[[], Awaitable[list[str]]],
    prune_before: Callable[[datetime], Awaitable[None]],
    storage_allowed: Callable[[], Awaitable[tuple[bool, dict[str, Any]]]],
    capture_quote: Callable[[str], Awaitable[dict[str, Any]]],
    observe_completed: Callable[[asyncio.Task[dict[str, Any]], set[asyncio.Task[dict[str, Any]]], str], None],
    interval_seconds: Callable[[], float],
    max_in_flight: Callable[[], int],
    retention_days: Callable[[], int],
) -> None:
    """Start at most one rotated cross-check per interval in special windows.

    The service owns lifecycle-only concerns.  It does not fetch providers or
    mutate the database itself; the application injects those responsibilities
    so the loop can be tested and moved without changing strategy semantics.
    """
    loop = asyncio.get_running_loop()
    next_started_at = loop.time()
    refresh_at = 0.0
    symbols: list[str] = []
    cursor = 0
    in_flight: set[asyncio.Task[dict[str, Any]]] = set()
    pruned_on: date | None = None
    try:
        while True:
            await asyncio.sleep(max(0.0, next_started_at - loop.time()))
            local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
            active, _ = await realtime_session()
            if not active or not high_frequency_window(local):
                next_started_at = loop.time() + 1.0
                continue
            if loop.time() >= refresh_at:
                symbols = await load_symbols()
                refresh_at = loop.time() + 30.0
                if symbols:
                    cursor %= len(symbols)
            if pruned_on != local.date():
                await prune_before(datetime.now(timezone.utc) - timedelta(days=retention_days()))
                pruned_on = local.date()
            allowed, _storage = await storage_allowed()
            if not allowed:
                # Preserve watched-price/risk scans; only this optional raw
                # cross-check pauses under the shared storage guard.
                next_started_at = loop.time() + interval_seconds()
                continue
            if len(in_flight) < max_in_flight():
                symbol, cursor = fast_quote_rotation_slot(symbols, cursor)
                if symbol is not None:
                    task = asyncio.create_task(capture_quote(symbol))
                    in_flight.add(task)
                    task.add_done_callback(
                        lambda completed: observe_completed(completed, in_flight, "intraday Super GET fast quote")
                    )
            # No catch-up bursts after proxy latency or storage recovery.
            next_started_at = loop.time() + interval_seconds()
    finally:
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)


__all__ = ["cross_source_confirmation", "fast_quote_rotation_slot", "run_intraday_fast_quote_loop"]

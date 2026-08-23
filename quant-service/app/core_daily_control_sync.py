"""Explicit-symbol, same-day Tushare control-plane refresh.

This small compatibility path serves the daily pipeline's configured core
universe.  It never expands to a historical or all-market backfill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import HTTPException


@dataclass(frozen=True)
class CoreDailyControlDependencies:
    resolve_symbols: Callable[[list[str]], Awaitable[list[str]]]
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]]
    request: Callable[..., Any]


async def sync(
    as_of_date: date,
    requested_symbols: list[str] | None,
    deps: CoreDailyControlDependencies,
) -> dict[str, Any]:
    """Refresh only calendar plus four controls for the resolved core symbols."""
    symbols = [symbol for symbol in await deps.resolve_symbols(requested_symbols or []) if symbol != "000300.SH"]
    if not symbols:
        return {"status": "disabled", "reason": "no explicit equity universe", "requests": []}
    stamp = as_of_date.strftime("%Y%m%d")
    calendar_start = date(as_of_date.year, 1, 1).strftime("%Y%m%d")
    calendar_end = date(as_of_date.year, 12, 31).strftime("%Y%m%d")
    requests = [deps.request(
        api_name="trade_cal",
        params={"exchange": "SSE", "start_date": calendar_start, "end_date": calendar_end},
        max_rows=400,
    )]
    for symbol in symbols:
        shared = {"ts_code": symbol, "start_date": stamp, "end_date": stamp}
        requests.extend([
            deps.request(api_name="daily_basic", params=shared, max_rows=10),
            deps.request(api_name="adj_factor", params=shared, max_rows=10),
            deps.request(api_name="stk_limit", params=shared, max_rows=10),
            deps.request(api_name="suspend_d", params={"ts_code": symbol, "trade_date": stamp}, max_rows=10),
        ])
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for request in requests:
        try:
            results.append(await deps.fetch_catalog(request))
        except HTTPException:
            failures.append(request.api_name)
    return {"status": "completed" if not failures else "partial", "symbols": symbols, "requests": results, "failures": failures}


__all__ = ["CoreDailyControlDependencies", "sync"]

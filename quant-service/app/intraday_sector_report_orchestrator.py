"""Bounded external collection for the intraday sector report."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Awaitable, Callable


async def run(
    request: Any,
    *,
    run_public_blocking: Callable[..., Awaitable[Any]],
    board_flow: Callable[[str], Any],
    all_a_snapshot: Callable[[], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]],
    build_membership_report: Callable[..., Awaitable[Any]],
    hydrate_members: Callable[[str, list[dict[str, Any]], int], Awaitable[list[dict[str, Any]]]],
    member_symbol: Callable[[dict[str, Any]], str | None],
    number: Callable[[Any], float | None],
    exchange_date: Callable[[], date],
    safe_error: Callable[[str, int], str],
    executor_saturated_error: type[Exception],
    provider_error: type[Exception],
) -> dict[str, Any]:
    kinds = ("concept", "industry") if request.kind == "all" else (request.kind,)
    try:
        collected = await asyncio.gather(
            *(run_public_blocking(board_flow, kind, timeout_seconds=20) for kind in kinds),
            all_a_snapshot(),
        )
        *flow_parts, all_a_result = collected
        quote_rows, _snapshot_status = all_a_result
    except executor_saturated_error as error:
        return {"status": "blocked", "reason": safe_error(str(error), 300),
                "sources": {"eastmoney": "not_started", "fuyao": "not_started"}}
    except asyncio.TimeoutError:
        return {"status": "blocked", "reason": "Eastmoney/Fuyao live request exceeded 20 second budget",
                "sources": {"eastmoney": "attempted", "fuyao": "attempted"}}
    except (provider_error, ValueError) as error:
        return {"status": "blocked", "reason": safe_error(str(error), 500),
                "sources": {"eastmoney": "attempted", "fuyao": "attempted"}}
    quotes: dict[str, dict[str, Any]] = {}
    for row in quote_rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            quotes[symbol] = {
                "symbol": symbol, "name": row.get("name"), "pct_change": number(row.get("pct_change")),
                "turnover": number(row.get("turnover")),
            }
    hydration: dict[str, list[dict[str, Any]]] = {}
    if request.hydrate_top_boards:
        for kind, flows in zip(kinds, flow_parts, strict=True):
            hydration[kind] = await hydrate_members(kind, flows, request.hydrate_top_boards)
    report, coverage, sector_context, stock_context, realtime_context = await build_membership_report(
        kinds, list(flow_parts), quotes, request.top_stocks, exchange_date(),
    )
    report.sort(key=lambda item: (item["taxonomy_key"], -(item["net_inflow"] or 0), item["label"]))
    return {
        "status": "completed", "rank_by": "eastmoney_board_flow_then_fuyao_turnover", "decision_eligible": False,
        "coverage": coverage, "items": report, "_runtime_quotes": quotes, "membership_hydration": hydration,
        "tushare_context": {
            "sector_close_flow": sector_context, "stock_daily_flow": stock_context,
            "realtime_probe": realtime_context,
            "semantics": "Tushare sector/stock flow is close-daily context; rt_* is a bounded candidate validation source, not a full-market scan.",
        },
    }


__all__ = ["run"]

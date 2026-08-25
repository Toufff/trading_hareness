"""Bounded public-quote capture for an explicit intraday watchlist."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class WatchQuoteCapture:
    quotes: dict[str, dict[str, Any]]
    all_a_rows: list[dict[str, Any]]
    all_a_snapshot_status: dict[str, Any]
    fresh_watch_rows: list[dict[str, Any]]
    sina_watch_rows: list[dict[str, Any]]
    eastmoney_watch_flow_rows: list[dict[str, Any]]
    latency_ms: int


@dataclass(frozen=True)
class WatchQuoteCaptureDependencies:
    now: Callable[[], float]
    all_a_snapshot: Callable[[], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]]
    tencent_watch_quotes: Callable[..., Awaitable[list[dict[str, Any]]]]
    sina_quotes: Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
    eastmoney_watch_flows: Callable[..., Awaitable[list[dict[str, Any]]]]
    quote_from_all_a: Callable[[dict[str, Any]], dict[str, Any] | None]
    merge_eastmoney_flows: Callable[[dict[str, dict[str, Any]], list[dict[str, Any]]], Any]
    annotate_percentiles: Callable[[dict[str, dict[str, Any]]], Any]
    annotate_flow_provenance: Callable[[dict[str, dict[str, Any]], dict[str, Any]], Any]
    merge_watch_prices: Callable[[dict[str, dict[str, Any]], list[dict[str, Any]]], Any]
    merge_sina_prices: Callable[[dict[str, dict[str, Any]], list[dict[str, Any]]], Any]
    quote_freshness: Callable[[dict[str, Any], datetime, float], dict[str, Any]]
    consume_background_exception: Callable[[Any], Any]
    safe_error: Callable[[str, int], str]
    executor_saturated_error: type[Exception]
    watch_quote_errors: tuple[type[Exception], ...]
    all_a_snapshot_errors: tuple[type[Exception], ...]


async def capture_watch_quotes(
    symbols: list[str], observed_at: datetime, quote_timestamp_slo_seconds: float,
    dependencies: WatchQuoteCaptureDependencies,
) -> WatchQuoteCapture:
    """Capture bounded quote evidence without promoting fallback prices.

    The all-A task is deliberately allowed only a two-second scan budget.  If
    it finishes later its exception is consumed, but it never delays direct
    watch prices or turns a stale cross-section into a decision quote.
    """
    started_at = dependencies.now()
    timeout_or_all_a_errors = (asyncio.TimeoutError, *dependencies.all_a_snapshot_errors)
    all_a_task = asyncio.create_task(dependencies.all_a_snapshot())
    all_a_task.add_done_callback(dependencies.consume_background_exception)
    try:
        fresh_watch_rows = await dependencies.tencent_watch_quotes(symbols, max_symbols=40)
    except dependencies.watch_quote_errors:
        fresh_watch_rows = []
    try:
        sina_watch_rows = await dependencies.sina_quotes(symbols) if not fresh_watch_rows else []
    except dependencies.watch_quote_errors:
        sina_watch_rows = []
    try:
        all_a_rows, all_a_snapshot_status = await asyncio.wait_for(asyncio.shield(all_a_task), timeout=2.0)
    except dependencies.executor_saturated_error as error:
        detail = dependencies.safe_error(str(error), 300)
        all_a_rows, all_a_snapshot_status = [], {"status": "unavailable", "error": detail}
    except timeout_or_all_a_errors as error:
        detail = dependencies.safe_error(str(error), 300)
        all_a_rows, all_a_snapshot_status = [], {"status": "unavailable", "error": detail}
    quotes = {item["symbol"]: item for row in all_a_rows if (item := dependencies.quote_from_all_a(row)) is not None}
    eastmoney_watch_flow_rows: list[dict[str, Any]] = []
    if not all_a_rows:
        try:
            eastmoney_watch_flow_rows = await asyncio.wait_for(
                dependencies.eastmoney_watch_flows(symbols, max_symbols=40), timeout=2.0,
            )
        except (asyncio.TimeoutError, *dependencies.watch_quote_errors) as error:
            all_a_snapshot_status = {
                **all_a_snapshot_status,
                "eastmoney_watch_fallback_error": dependencies.safe_error(str(error), 300),
            }
        else:
            if eastmoney_watch_flow_rows:
                dependencies.merge_eastmoney_flows(quotes, eastmoney_watch_flow_rows)
                all_a_snapshot_status = {
                    "status": "fresh", "age_seconds": 0.0,
                    "source": "eastmoney_watch_flow_batch", "scope": "explicit_watchlist_only",
                    "cross_sectional": False,
                    "semantics": "watchlist_public_flow_proxy_not_exchange_order_flow",
                    "fallback_from": "fuyao_ths_all_a_snapshot",
                    "matched_symbols": len(eastmoney_watch_flow_rows),
                }
    if all_a_snapshot_status.get("cross_sectional", True):
        dependencies.annotate_percentiles(quotes)
    dependencies.annotate_flow_provenance(quotes, all_a_snapshot_status)
    dependencies.merge_watch_prices(quotes, fresh_watch_rows)
    dependencies.merge_sina_prices(quotes, sina_watch_rows)
    for quote in quotes.values():
        quote["price_freshness"] = dependencies.quote_freshness(
            quote, observed_at, quote_timestamp_slo_seconds,
        )
    return WatchQuoteCapture(
        quotes=quotes, all_a_rows=all_a_rows, all_a_snapshot_status=all_a_snapshot_status,
        fresh_watch_rows=fresh_watch_rows, sina_watch_rows=sina_watch_rows,
        eastmoney_watch_flow_rows=eastmoney_watch_flow_rows,
        latency_ms=round((dependencies.now() - started_at) * 1000),
    )


__all__ = ["WatchQuoteCapture", "WatchQuoteCaptureDependencies", "capture_watch_quotes"]

"""Bounded public-quote capture for an explicit intraday watchlist."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from .platform.evidence_contracts import materialize_evidence_status


@dataclass(frozen=True)
class WatchQuoteCapture:
    quotes: dict[str, dict[str, Any]]
    all_a_rows: list[dict[str, Any]]
    all_a_snapshot_status: dict[str, Any]
    fresh_watch_rows: list[dict[str, Any]]
    sina_watch_rows: list[dict[str, Any]]
    eastmoney_watch_flow_rows: list[dict[str, Any]]
    eastmoney_watch_flow_status: dict[str, Any]
    derived_flow_status: dict[str, Any]
    latency_ms: int


@dataclass(frozen=True)
class WatchQuoteCaptureDependencies:
    now: Callable[[], float]
    all_a_snapshot: Callable[[], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]]
    tencent_watch_quotes: Callable[..., Awaitable[list[dict[str, Any]]]]
    sina_quotes: Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
    eastmoney_watch_flows: Callable[..., Awaitable[list[dict[str, Any]]]]
    watch_flow_reference: Callable[[list[str], datetime], Awaitable[dict[str, dict[str, Any]]]]
    watch_volume_fallback: Callable[[list[str]], Awaitable[dict[str, float]]]
    derive_flow_metrics: Callable[..., dict[str, dict[str, float]]]
    apply_derived_flow_metrics: Callable[
        [dict[str, dict[str, Any]], dict[str, dict[str, float]]], dict[str, dict[str, str]]
    ]
    derived_flow_divergence: Callable[..., dict[str, Any]]
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
    watch_flow_reference_errors: tuple[type[Exception], ...]
    all_a_snapshot_errors: tuple[type[Exception], ...]


async def _apply_derived_flow_metrics(
    quotes: dict[str, dict[str, Any]], reference_task: "asyncio.Task[dict[str, dict[str, Any]]]",
    observed_at: datetime, dependencies: WatchQuoteCaptureDependencies,
) -> dict[str, Any]:
    """Overlay the licensed derived metrics over the public Eastmoney values.

    This runs after the Eastmoney merge on purpose: the derived value wins
    where it exists, and any field it cannot derive - always including
    ``main_net_inflow``, which no licensed route supplies - keeps whatever the
    public endpoint returned.  A reference read that fails or exceeds the scan
    budget degrades to exactly today's Eastmoney-only behaviour.
    """
    try:
        reference = await asyncio.wait_for(asyncio.shield(reference_task), timeout=2.0)
    except (asyncio.TimeoutError, *dependencies.watch_flow_reference_errors) as error:
        return materialize_evidence_status(
            "fuyao_ths_derived_watch_flow",
            {"status": "unavailable", "error": dependencies.safe_error(str(error), 300)},
        )
    # The derivation needs cumulative volume, normally carried by the all-A
    # snapshot.  On 2026-08-26 13:30 that snapshot and the Eastmoney basket
    # failed in the same scan, leaving the watchlist with prices but no flow at
    # all.  A single batched realtime quote covers the whole basket in well
    # under a second and is independent of both, so it is used - only when the
    # snapshot supplied nothing, never on the normal path.
    volume_fallback: dict[str, float] = {}
    if quotes and not any(quote.get("volume") for quote in quotes.values()):
        try:
            volume_fallback = await asyncio.wait_for(
                dependencies.watch_volume_fallback(sorted(quotes)), timeout=3.0,
            )
        except (asyncio.TimeoutError, *dependencies.watch_quote_errors) as error:
            volume_fallback = {}
            fallback_error = dependencies.safe_error(str(error), 200)
        else:
            fallback_error = None
        for symbol, volume in volume_fallback.items():
            if symbol in quotes and not quotes[symbol].get("volume"):
                quotes[symbol]["volume"] = volume
                quotes[symbol]["volume_source"] = "promax_rt_k_batch"
    else:
        fallback_error = None
    derived = dependencies.derive_flow_metrics(quotes, reference, observed_at=observed_at)
    sources = dependencies.apply_derived_flow_metrics(quotes, derived)
    divergence = dependencies.derived_flow_divergence(quotes, derived)
    field_counts = {
        field: sum(1 for labels in sources.values() if labels.get(field) == "fuyao_ths_derived")
        for field in ("volume_ratio", "turnover_rate")
    }
    return materialize_evidence_status(
        "fuyao_ths_derived_watch_flow",
        {"status": "fresh" if derived else "unavailable", "age_seconds": 0.0,
         "source": "fuyao_ths_all_a_snapshot_volume_with_local_float_shares",
         "reference_symbols": len(reference), "derived_symbols": len(derived),
         "derived_field_symbols": field_counts,
         "main_net_inflow_source": "eastmoney_watch_flow_only_no_licensed_equivalent",
         "volume_fallback_symbols": len(volume_fallback),
         "volume_fallback_error": fallback_error,
         "eastmoney_agreement": divergence},
    )


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
    # This is deliberately a single bounded request for the explicit
    # watchlist, started beside the all-A snapshot.  It is research
    # corroboration only: its values never represent an all-market ranking.
    eastmoney_task = asyncio.create_task(dependencies.eastmoney_watch_flows(symbols, max_symbols=40))
    eastmoney_task.add_done_callback(dependencies.consume_background_exception)
    # Local reference for the derived flow metrics.  It is a small indexed read
    # of already-persisted end-of-day rows, started here so it overlaps the
    # provider calls instead of extending the scan budget.
    reference_task = asyncio.create_task(dependencies.watch_flow_reference(symbols, observed_at))
    reference_task.add_done_callback(dependencies.consume_background_exception)
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
    all_a_snapshot_status = materialize_evidence_status("fuyao_all_a_snapshot", all_a_snapshot_status)
    quotes = {item["symbol"]: item for row in all_a_rows if (item := dependencies.quote_from_all_a(row)) is not None}
    eastmoney_watch_flow_rows: list[dict[str, Any]] = []
    eastmoney_watch_flow_status = materialize_evidence_status(
        "eastmoney_watch_flow", {"status": "unavailable"}, research_confirmation_only=True,
    )
    try:
        eastmoney_watch_flow_rows = await asyncio.wait_for(asyncio.shield(eastmoney_task), timeout=2.0)
    except (asyncio.TimeoutError, *dependencies.watch_quote_errors) as error:
        eastmoney_watch_flow_status["error"] = dependencies.safe_error(str(error), 300)
    else:
        eastmoney_watch_flow_status = materialize_evidence_status(
            "eastmoney_watch_flow",
            {"status": "fresh", "age_seconds": 0.0, "source": "eastmoney_watch_flow_batch",
             "matched_symbols": len(eastmoney_watch_flow_rows)},
            research_confirmation_only=True,
        )
    if all_a_rows and all_a_snapshot_status.get("cross_sectional", True):
        dependencies.annotate_percentiles(quotes)
    dependencies.annotate_flow_provenance(quotes, all_a_snapshot_status)
    if eastmoney_watch_flow_rows:
        dependencies.merge_eastmoney_flows(quotes, eastmoney_watch_flow_rows)
        eastmoney_quotes = {
            str(row.get("ts_code") or ""): quotes[str(row.get("ts_code") or "")]
            for row in eastmoney_watch_flow_rows if str(row.get("ts_code") or "") in quotes
        }
        dependencies.annotate_flow_provenance(eastmoney_quotes, eastmoney_watch_flow_status)
    dependencies.merge_watch_prices(quotes, fresh_watch_rows)
    dependencies.merge_sina_prices(quotes, sina_watch_rows)
    # Deliberately after the price merges: when the all-A snapshot fails
    # outright these merges are what put the watch basket into ``quotes`` at
    # all, and without them the volume fallback would have nothing to attach to.
    derived_flow_status = await _apply_derived_flow_metrics(
        quotes, reference_task, observed_at, dependencies,
    )
    for quote in quotes.values():
        quote["price_freshness"] = dependencies.quote_freshness(
            quote, observed_at, quote_timestamp_slo_seconds,
        )
    return WatchQuoteCapture(
        quotes=quotes, all_a_rows=all_a_rows, all_a_snapshot_status=all_a_snapshot_status,
        fresh_watch_rows=fresh_watch_rows, sina_watch_rows=sina_watch_rows,
        eastmoney_watch_flow_rows=eastmoney_watch_flow_rows,
        eastmoney_watch_flow_status=eastmoney_watch_flow_status,
        derived_flow_status=derived_flow_status,
        latency_ms=round((dependencies.now() - started_at) * 1000),
    )


__all__ = ["WatchQuoteCapture", "WatchQuoteCaptureDependencies", "capture_watch_quotes"]

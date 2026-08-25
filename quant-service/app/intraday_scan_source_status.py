"""Deterministic evidence projection for one intraday scan's source status."""

from __future__ import annotations

from typing import Any


def build_scan_source_status(
    *,
    selected_symbols: list[str],
    quotes: dict[str, dict[str, Any]],
    all_a_rows: list[dict[str, Any]],
    fresh_watch_rows: list[dict[str, Any]],
    sina_watch_rows: list[dict[str, Any]],
    eastmoney_watch_flow_rows: list[dict[str, Any]],
    eastmoney_watch_flow_status: dict[str, Any],
    all_a_snapshot_status: dict[str, Any],
    surge_source: dict[str, Any],
    priority_symbols: list[str],
    rotation_pool_size: int,
    rotation_start_offset: int,
    next_rotation_offset: int,
    tushare_minutes: dict[str, dict[str, Any]],
    fast_confirmations: dict[str, dict[str, Any]],
    board_cache_evidence: dict[str, Any],
    quote_timestamp_slo_seconds: float,
) -> dict[str, Any]:
    """Describe source coverage without inferring unavailable data as fresh."""
    fast_status_counts: dict[str, int] = {}
    for item in fast_confirmations.values():
        status = str(item.get("status") or "unknown")
        fast_status_counts[status] = fast_status_counts.get(status, 0) + 1
    direct_watch_symbols = {
        str(row.get("ts_code") or "") for row in fresh_watch_rows
        if str(row.get("ts_code") or "") in selected_symbols
    }
    sina_watch_symbols = {
        str(row.get("ts_code") or "") for row in sina_watch_rows
        if str(row.get("ts_code") or "") in selected_symbols
    }
    all_a_watch_symbols = {
        symbol for symbol in selected_symbols
        if (quotes.get(symbol) or {}).get("price_source") == "fuyao_ths_all_a_snapshot"
    }
    direct_watch_count = len(direct_watch_symbols)
    fresh_direct_watch_count = sum(
        1 for symbol in direct_watch_symbols
        if ((quotes.get(symbol) or {}).get("price_freshness") or {}).get("status") == "fresh"
    )
    direct_status = (
        "completed" if fresh_direct_watch_count == len(selected_symbols)
        else "partial" if direct_watch_count or all_a_rows else "unavailable"
    )
    return {
        "fuyao": {
            "status": "completed" if all_a_rows else "unavailable", "rows": len(all_a_rows),
            "matched": sum(symbol in quotes for symbol in selected_symbols),
            "all_a_snapshot": all_a_snapshot_status,
            "all_a_only_watch_quote_symbols": len(all_a_watch_symbols),
        },
        "tencent_watch": {
            "status": direct_status,
            "fresh_watch_quote_rows": len(fresh_watch_rows),
            "fresh_watch_quote_symbols": direct_watch_count,
            "decision_eligible_watch_quote_symbols": fresh_direct_watch_count,
            "stale_or_unstamped_direct_watch_quote_symbols": direct_watch_count - fresh_direct_watch_count,
            "quote_timestamp_slo_seconds": quote_timestamp_slo_seconds,
            "sina_fallback_watch_quote_symbols": len(sina_watch_symbols),
            "missing_direct_watch_quote_symbols": len(selected_symbols) - direct_watch_count,
            "sina_watch_quote_rows": len(sina_watch_rows),
        },
        "eastmoney_watch_flow": {
            **eastmoney_watch_flow_status,
            "status": "completed" if eastmoney_watch_flow_status.get("status") == "fresh" else "unavailable",
            "rows": len(eastmoney_watch_flow_rows), "scope": "explicit_watchlist_only",
            "percentiles": "not_computed", "research_confirmation_only": True,
        },
        "tencent_minute_context": surge_source,
        "tushare_rt_min": {
            "requested": priority_symbols,
            "items": {symbol: item["source"] for symbol, item in tushare_minutes.items()},
            "rotation_pool_size": rotation_pool_size,
            "rotation_start_offset": rotation_start_offset,
            "next_rotation_offset": next_rotation_offset,
        },
        "tushare_rt_k_fast": {
            "status_counts": fast_status_counts, "max_age_seconds": 30,
            "cadence": "one request start per second in selected windows",
        },
        "eastmoney_board_flow": board_cache_evidence,
        "post_close_lhb_cninfo": "context only; never used in same-day intraday signal",
    }


__all__ = ["build_scan_source_status"]

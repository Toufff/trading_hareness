"""Stored-only strategy-context projections.

All functions in this module read previously persisted evidence.  They do not
fetch providers or alter a decision score, which keeps strategy review and
decision assembly explicit about its point-in-time data boundary.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable


def index_breadth_context(
    connection: Any,
    as_of_date: date,
    session: str,
    observed_at: datetime,
    *,
    index_symbols: tuple[str, ...],
    index_regime: Callable[[list[dict[str, Any]]], dict[str, Any]],
    number: Callable[[Any], float],
) -> dict[str, Any]:
    """Return only index/breadth evidence available by the review checkpoint."""
    snapshot = connection.execute(
        """SELECT observed_at,status,coverage,summary,quality_flags,source_summary
             FROM quant.market_snapshot_runs
             WHERE exchange_date=%s AND session=%s AND observed_at<=%s
             ORDER BY observed_at DESC LIMIT 1""",
        (as_of_date, session, observed_at),
    ).fetchone()
    index = connection.execute(
        """SELECT trading_date,close,pre_close,available_at FROM quant.canonical_bars_daily
             WHERE symbol='000300.SH' AND trading_date<=%s AND available_at<=%s
             ORDER BY trading_date DESC LIMIT 1""",
        (as_of_date, observed_at),
    ).fetchone()
    index_rows = connection.execute(
        """WITH ranked AS (
               SELECT symbol,trading_date,open,high,low,close,volume,available_at,
                      row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS recent_rank
                 FROM quant.market_bars_daily
                WHERE symbol=ANY(%s) AND trading_date<=%s AND available_at<=%s
           )
           SELECT symbol,trading_date,open,high,low,close,volume,available_at
             FROM ranked WHERE recent_rank<=30 ORDER BY symbol,trading_date DESC""",
        (list(index_symbols), as_of_date, observed_at),
    ).fetchall()
    context: dict[str, Any] = {
        "index": None,
        "multi_index_regime": index_regime([dict(row) for row in index_rows]),
        "breadth": None,
        "quality_flags": [],
    }
    latest_index_dates = {item["symbol"]: item["trading_date"] for item in context["multi_index_regime"]["items"]}
    if any(value != str(as_of_date) for value in latest_index_dates.values()) or len(latest_index_dates) < 3:
        context["quality_flags"].append("multi_index_close_context_not_current")
    if index:
        close, pre_close = number(index["close"]), number(index["pre_close"])
        context["index"] = {
            "symbol": "000300.SH", "trading_date": str(index["trading_date"]), "close": close,
            "change_pct": round((close / pre_close - 1) * 100, 4) if pre_close else None,
            "available_at": index["available_at"].isoformat(), "role": "daily close context, not intraday index quote",
        }
        if index["trading_date"] != as_of_date:
            context["quality_flags"].append("index_not_current_exchange_date")
    else:
        context["quality_flags"].append("missing_index_context")
    if snapshot and int((snapshot["summary"] or {}).get("priced_symbols") or 0) > 0:
        summary = dict(snapshot["summary"] or {})
        advancing, declining = int(summary.get("advancers") or 0), int(summary.get("decliners") or 0)
        known = advancing + declining
        advance_share = advancing / known if known else None
        breadth_state = "broad_positive" if advance_share is not None and advance_share >= 0.60 else \
                        "broad_negative" if advance_share is not None and advance_share <= 0.40 else "mixed"
        context["breadth"] = {
            "observed_at": snapshot["observed_at"].isoformat(), "status": snapshot["status"],
            "coverage": number(snapshot["coverage"]), "advancers": advancing, "decliners": declining,
            "unchanged": int(summary.get("unchanged") or 0),
            "advance_share": round(advance_share, 4) if advance_share is not None else None,
            "median_change_pct": summary.get("median_change_pct"), "state": breadth_state,
        }
        context["quality_flags"].extend(list(snapshot["quality_flags"] or []))
    else:
        context["quality_flags"].append("missing_usable_breadth_snapshot")
    return context


def event_context(database: Any, symbols: list[str], observed_at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read local next-session event context that was available at the snapshot."""
    if not symbols:
        return {}
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT symbol,event_type,title,available_at
                 FROM quant.market_events
                WHERE symbol=ANY(%s) AND available_at<=%s
                  AND event_type=ANY(%s)
                ORDER BY available_at DESC LIMIT 100""",
            (symbols, observed_at, ["lhb_event", "strong_pool", "limit_up_pool", "previous_limit_pool", "limit_open_pool"]),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(dict(row))
    return grouped


def tushare_lhb_context(database: Any, symbols: list[str], observed_at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read post-close LHB evidence, never treating it as same-day signal input."""
    if not symbols:
        return {}
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT api_name,row_data,available_at
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('top_list','top_inst') AND available_at<=%s
                  AND row_data->>'ts_code'=ANY(%s)
                ORDER BY available_at DESC,record_index LIMIT 100""",
            (observed_at, symbols),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row["row_data"])
        symbol = str(payload.get("ts_code") or "")
        if symbol:
            grouped.setdefault(symbol, []).append({"api_name": row["api_name"], "available_at": row["available_at"], "row": payload})
    return grouped


def source_readiness(
    database: Any,
    observed_at: datetime,
    *,
    provider_status: Callable[[], list[dict[str, Any]]],
    json_safe: Callable[[Any], Any],
) -> dict[str, Any]:
    """Expose local freshness/ownership without inventing provider parity."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT provider_key,capability,last_success_at,last_failure_at,last_row_count,consecutive_failures
                 FROM quant.provider_health
                WHERE provider_key IN ('akshare','eastmoney_free','tencent_free','tushare_primary','tushare_super_sdk','tushare_super_get')
                ORDER BY provider_key,capability"""
        ).fetchall()
        event_rows = connection.execute(
            """SELECT source,event_type,max(available_at) latest_available_at,count(*)::int rows
                 FROM quant.market_events WHERE available_at<=%s
                GROUP BY source,event_type ORDER BY source,event_type""",
            (observed_at,),
        ).fetchall()
    providers: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = providers.setdefault(str(row["provider_key"]), {"capabilities": []})
        provider["capabilities"].append(json_safe(dict(row)))
    return {
        "providers": providers,
        "post_close_event_inventory": json_safe([dict(row) for row in event_rows]),
        "xinhua_finance": {
            "status": "configured_contract_required" if any(item.get("provider_key") == "xinhua_finance" and item.get("configured") for item in provider_status()) else "not_configured",
            "reason": "requires the licensed API URL, authentication scheme and response-field contract; no public endpoint is guessed",
        },
    }


__all__ = ["event_context", "index_breadth_context", "source_readiness", "tushare_lhb_context"]

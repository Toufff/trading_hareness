"""Canonical feature snapshot materialization on a caller-owned transaction."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time
from statistics import mean
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .stable_json import stable_dumps, stable_json

from .research_prices import adjusted_bars


def materialize_feature_snapshot(
    connection: Any, as_of_date: date, universe_key: str, *, feature_version: str,
    number: Callable[[Any], float], market_regime: Callable[[Any, date], str],
    analyst_text_factor_summary: Callable[[Any, date], dict[str, Any]],
    latest_tushare_row: Callable[[Any, str, str, date], dict[str, Any] | None],
    analyst_feature: Callable[[Any, str, date], dict[str, Any]],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Materialize one universe's feature snapshot as of ``as_of_date``.

    Both batched reads below are point-in-time bounded: ``trading_date`` is
    strictly before ``as_of_date`` (a same-day row is that date's own outcome,
    not a prior-session feature), and ``available_at`` must not be later than
    ``observed_at`` -- a bar can be re-dated by a later backfill/correction
    whose ``available_at`` postdates a historical replay's own ``as_of_date``,
    which a ``trading_date`` bound alone would not catch.  ``observed_at``
    defaults to the end of ``as_of_date`` (Asia/Shanghai), which is exactly
    "any time up to and including that date" for a live/current-day snapshot
    and the correct fail-closed default for a historical replay call that did
    not pass one explicitly.
    """
    observed_at = observed_at or datetime.combine(as_of_date, time(23, 59, 59), tzinfo=ZoneInfo("Asia/Shanghai"))
    members = connection.execute(
        """SELECT m.symbol,i.name,i.industry,i.is_st FROM quant.universe_members m
           JOIN quant.instruments i ON i.symbol=m.symbol
           WHERE m.universe_key=%s AND m.enabled ORDER BY m.priority,m.symbol""", (universe_key,)
    ).fetchall()
    if not members:
        raise ValueError(f"universe {universe_key} has no enabled symbols")
    regime = market_regime(connection, as_of_date)
    analyst_context = analyst_text_factor_summary(connection, as_of_date)

    # Batched once for every member instead of two extra round trips per
    # symbol (previously ~2 * len(members) queries for a full universe).
    # The per-symbol "most recent N rows as of as_of_date" shape is kept
    # identical via a ranked CTE; only the loop structure below changed, not
    # the date condition, so a later date-boundary fix stays a one-line edit.
    all_symbols = [str(member["symbol"]) for member in members]
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        """WITH ranked AS (
               SELECT symbol,trading_date,close,high,low,volume,amount,adj_factor,is_suspended,
                      limit_up,limit_down,selected_provider,
                      row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                 FROM quant.canonical_bars_daily
                WHERE symbol=ANY(%s) AND trading_date<%s AND available_at<=%s
           )
           SELECT symbol,trading_date,close,high,low,volume,amount,adj_factor,is_suspended,limit_up,limit_down,selected_provider
             FROM ranked WHERE rn<=60 ORDER BY symbol,trading_date DESC""",
        (all_symbols, as_of_date, observed_at),
    ).fetchall():
        bars_by_symbol.setdefault(str(row["symbol"]), []).append(dict(row))
    fundamentals_by_symbol: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """WITH ranked AS (
               SELECT symbol,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv,
                      row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                 FROM quant.daily_fundamentals
                WHERE symbol=ANY(%s) AND trading_date<%s AND available_at<=%s
           )
           SELECT symbol,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv FROM ranked WHERE rn=1""",
        (all_symbols, as_of_date, observed_at),
    ).fetchall():
        row = dict(row)
        symbol_key = str(row.pop("symbol"))
        fundamentals_by_symbol[symbol_key] = row

    # Point-in-time ST status: ``instrument_lifecycle_evidence`` is provider
    # evidence with its own observed/available timestamps, unlike
    # ``instruments.is_st`` (the current-state join already in ``members``,
    # which a historical replay must not read as "known" back then).  Not
    # every symbol has lifecycle evidence yet, so this only overrides
    # ``member["is_st"]`` where a point-in-time row actually exists; symbols
    # without one keep today's current-state value as an explicit fallback
    # rather than silently reporting an unknown status as not-ST.
    is_st_by_symbol: dict[str, bool] = {}
    for row in connection.execute(
        """WITH ranked AS (
               SELECT symbol,is_st,
                      row_number() OVER (PARTITION BY symbol ORDER BY status_date DESC,observed_at DESC) AS rn
                 FROM quant.instrument_lifecycle_evidence
                WHERE symbol=ANY(%s) AND status_date<%s AND available_at<=%s AND is_st IS NOT NULL
           )
           SELECT symbol,is_st FROM ranked WHERE rn=1""",
        (all_symbols, as_of_date, observed_at),
    ).fetchall():
        is_st_by_symbol[str(row["symbol"])] = bool(row["is_st"])

    items: list[dict[str, Any]] = []
    for member in members:
        symbol = str(member["symbol"])
        is_st = is_st_by_symbol.get(symbol, member["is_st"])
        bars = list(reversed(bars_by_symbol.get(symbol, [])))
        flags: list[str] = []
        if len(bars) < 21:
            flags.append("insufficient_history_20")
        if not bars:
            flags.append("missing_market_data")
            features: dict[str, Any] = {"symbol": symbol, "name": member["name"], "market_data_date": None, "bar_count": 0}
        else:
            bars = [dict(bar) for bar in bars]
            research_bars, adjustment_flags = adjusted_bars(bars)
            flags.extend(adjustment_flags)
            closes = [number(bar["close"]) for bar in bars]
            volumes = [number(bar["volume"]) for bar in bars]
            latest = bars[-1]
            latest_date = latest["trading_date"]
            if (as_of_date - latest_date).days > 5:
                flags.append("stale_market_data")
            if latest["is_suspended"]:
                flags.append("suspended")
            if is_st:
                flags.append("ST")
            if latest["limit_up"] is not None and number(latest["close"]) >= number(latest["limit_up"]):
                flags.append("limit_up_may_be_unbuyable")
            research_closes = ([number(bar["research_close"]) for bar in research_bars]
                               if research_bars is not None else [])
            has_research_price = bool(research_bars) and all(value > 0 for value in research_closes)
            sma5 = mean(research_closes[-5:]) if has_research_price and len(research_closes) >= 5 else None
            sma20 = mean(research_closes[-20:]) if has_research_price and len(research_closes) >= 20 else None
            return_5 = (research_closes[-1] / research_closes[-6] - 1
                        if has_research_price and len(research_closes) >= 6 and research_closes[-6] else None)
            return_20 = (research_closes[-1] / research_closes[-21] - 1
                         if has_research_price and len(research_closes) >= 21 and research_closes[-21] else None)
            volume_ratio = volumes[-1] / mean(volumes[-20:]) if len(volumes) >= 20 and mean(volumes[-20:]) else None
            # Keep raw ``close`` for audit/execution facts, but publish the
            # explicitly named research basis beside it.  Consumers must not
            # compare a raw close with an adjusted moving average across an
            # ex-rights date.
            features = {"symbol": symbol, "name": member["name"], "industry": member["industry"],
                        "market_data_date": str(latest_date), "bar_count": len(bars), "close": closes[-1],
                        "research_close": research_closes[-1] if has_research_price else None,
                        "sma_5": sma5, "sma_20": sma20, "return_5": return_5, "return_20": return_20,
                        "research_price_status": "complete" if has_research_price else "blocked",
                        "volume_ratio": volume_ratio, "selected_provider": latest["selected_provider"]}
        fundamental = fundamentals_by_symbol.get(symbol)
        if fundamental:
            features["fundamentals"] = {key: number(fundamental[key]) for key in fundamental.keys()}
        else:
            flags.append("missing_fundamentals")
        moneyflow = latest_tushare_row(connection, "moneyflow_dc", symbol, as_of_date)
        if moneyflow:
            features["moneyflow_dc"] = {"trade_date": moneyflow.get("trade_date"), "net_amount": number(moneyflow.get("net_amount")),
                                         "net_amount_rate": number(moneyflow.get("net_amount_rate")), "buy_elg_amount": number(moneyflow.get("buy_elg_amount")),
                                         "buy_sm_amount": number(moneyflow.get("buy_sm_amount"))}
        else:
            flags.append("missing_moneyflow_dc")
        standard_flow = latest_tushare_row(connection, "moneyflow", symbol, as_of_date)
        if standard_flow:
            features["moneyflow"] = {"trade_date": standard_flow.get("trade_date"), "net_mf_amount": number(standard_flow.get("net_mf_amount")),
                                      "net_mf_vol": number(standard_flow.get("net_mf_vol"))}
        features["analyst"] = analyst_feature(connection, symbol, as_of_date)
        features["analyst_market_context"] = analyst_context["market"]
        features["market_regime"] = regime
        items.append({"symbol": symbol, "features": features, "quality_flags": sorted(set(flags))})
    stable = stable_dumps(items)
    snapshot_key = hashlib.sha256(f"{feature_version}:{universe_key}:{as_of_date}:{stable}".encode()).hexdigest()
    for item in items:
        connection.execute(
            """INSERT INTO quant.feature_snapshots(snapshot_key,symbol,as_of_date,feature_version,features,quality_flags)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(snapshot_key,symbol,feature_version) DO NOTHING""",
            (snapshot_key, item["symbol"], as_of_date, feature_version,
             stable_json(item["features"]), stable_json(item["quality_flags"])),
        )
    return {"snapshot_key": snapshot_key, "as_of_date": str(as_of_date), "universe_key": universe_key,
            "feature_version": feature_version, "market_regime": regime, "items": items}


__all__ = ["materialize_feature_snapshot"]

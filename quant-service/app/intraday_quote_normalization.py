"""Pure source-labelled normalization for intraday watch quotes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo


_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)")


#: The three intraday quote sources that can populate ``quotes[symbol]["volume"]``
#: report cumulative volume in three different native units: LonghuVIP's
#: ``GetStockPanKou`` (unconfirmed by vendor documentation; treated as shares
#: here to match the exchange's own board-lot-adjacent convention until an
#: operator confirms it against a known symbol), Tencent's single-quote depth
#: feed (board lots, already labelled ``cumulative_volume_lot`` and never
#: written under the plain ``volume`` key), and Sina's realtime quote (shares).
#: A caller merging more than one of these into the same ``quotes[symbol]``
#: entry must consult ``volume_unit`` rather than assume any one convention.
LONGHU_WATCH_QUOTE_VOLUME_UNIT = "share"


def merge_longhu_watch_quotes(
    quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]], *, number: Callable[[Any], float | None],
) -> dict[str, dict[str, Any]]:
    """Overlay licensed watch quotes while preserving independently sourced flow.

    Longhu is the preferred direct price source when its exchange timestamp is
    present.  A malformed licensed row is ignored, leaving Tencent/Sina as the
    existing fallback path.

    ``volume`` is annotated with ``volume_unit`` because it is *not* the same
    unit as Tencent's board-lot cumulative volume or Sina's realtime share
    volume; a caller merging this quote with either of those sources must not
    treat the numbers as directly comparable without checking the unit.
    """
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        price, pre_close = number(row.get("price")), number(row.get("pre_close"))
        if not _SYMBOL.fullmatch(symbol) or price is None or price <= 0:
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        existing.update({
            "name": row.get("name") or existing.get("name"),
            "price": price,
            "pct_change": (
                round((price / pre_close - 1) * 100, 5)
                if pre_close and pre_close > 0 else number(row.get("pct_change"))
            ),
            "price_source": "longhuvip_watch_quote",
            # GetStockPanKou is a direct quote, not proof of Level-2 depth or
            # order-cancellation evidence.
            "price_observed_from_depth": False,
            "price_trade_date": row.get("trade_date"),
            "price_trade_time": row.get("trade_time"),
        })
        for key in ("volume", "amount", "turnover_rate", "volume_ratio"):
            value = number(row.get(key))
            if value is not None:
                existing[key] = value
        if row.get("ts_code") and number(row.get("volume")) is not None:
            existing["volume_unit"] = LONGHU_WATCH_QUOTE_VOLUME_UNIT
        existing["raw"] = {
            **(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}),
            "longhu_watch_quote": row,
        }
        quotes[symbol] = existing
    return quotes


def merge_watch_quote_prices(
    quotes: dict[str, dict[str, Any]], depth_rows: list[dict[str, Any]], *, number: Callable[[Any], float | None],
) -> dict[str, dict[str, Any]]:
    """Overlay dedicated Tencent batch prices without inventing flow fields."""
    for row in depth_rows:
        symbol = str(row.get("ts_code") or "")
        price, pre_close = number(row.get("price")), number(row.get("pre_close"))
        if not _SYMBOL.fullmatch(symbol) or price is None or price <= 0:
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        existing["price"] = price
        existing["pct_change"] = round((price / pre_close - 1) * 100, 5) if pre_close and pre_close > 0 else existing.get("pct_change")
        existing["price_source"] = "tencent_batched_watch_quote"
        existing["price_observed_from_depth"] = True
        existing["price_trade_time"] = row.get("trade_time")
        existing["raw"] = {**(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}), "watch_quote": row}
        quotes[symbol] = existing
    return quotes


def merge_sina_watch_quotes(
    quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]], *, number: Callable[[Any], float | None],
) -> dict[str, dict[str, Any]]:
    """Use Sina only as a price fallback; do not fabricate Tencent flow fields.

    A caller that already populated ``quotes[symbol]["price"]`` from a prior
    merge (Tencent's decision-eligible batch, or any other source) has a real
    price for that symbol.  Sina must never overwrite it: this function is a
    fallback for symbols with no price yet, not a second opinion that can
    silently replace an already-fresher, decision-eligible quote.
    """
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        price, pre_close = number(row.get("close")), number(row.get("pre_close"))
        if not _SYMBOL.fullmatch(symbol) or price is None or price <= 0:
            continue
        if (quotes.get(symbol) or {}).get("price") is not None:
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        existing["price"] = price
        existing["pct_change"] = round((price / pre_close - 1) * 100, 5) if pre_close and pre_close > 0 else existing.get("pct_change")
        existing["price_source"] = "sina_batched_watch_quote"
        existing["price_trade_date"] = row.get("trade_date")
        existing["price_trade_time"] = row.get("trade_time")
        existing["raw"] = {**(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}), "sina_watch_quote": row}
        quotes[symbol] = existing
    return quotes


def merge_eastmoney_watch_flows(
    quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]], *, number: Callable[[Any], float | None],
) -> dict[str, dict[str, Any]]:
    """Overlay bounded Eastmoney flow while preserving the actual price source."""
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        if not _SYMBOL.fullmatch(symbol):
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        for key in ("volume_ratio", "turnover_rate", "main_net_inflow", "main_net_inflow_ratio"):
            value = number(row.get(key))
            if value is not None:
                existing[key] = value
        existing["main_flow_percentile"] = None
        existing["raw"] = {**(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}),
                           "eastmoney_watch_flow": row.get("raw") if isinstance(row.get("raw"), dict) else row}
        quotes[symbol] = existing
    return quotes


def observation_source(quote: dict[str, Any] | None) -> str:
    """Return the actual provider associated with a persisted price frame."""
    source = str((quote or {}).get("price_source") or "")
    if source == "sina_batched_watch_quote":
        return "sina_free"
    if source == "tencent_batched_watch_quote":
        return "tencent_free"
    if source == "longhuvip_watch_quote":
        return "longhuvip"
    if source == "fuyao_ths_all_a_snapshot":
        return "fuyao_ths"
    return "unknown_realtime_source"


def exchange_time_status(quote: dict[str, Any] | None, observed_at: datetime, max_age_seconds: float) -> dict[str, Any]:
    """Classify an upstream price timestamp against one Shanghai-clock SLO."""
    payload = quote or {}
    compact = "".join(re.findall(r"\d", str(payload.get("price_trade_time") or "")))
    date_part = "".join(re.findall(r"\d", str(payload.get("price_trade_date") or "")))
    if len(compact) >= 14:
        candidate = compact[:14]
    elif len(date_part) == 8 and len(compact) >= 6:
        candidate = f"{date_part}{compact[:6]}"
    else:
        return {"status": "missing_timestamp", "max_age_seconds": max_age_seconds}
    try:
        exchange_at = datetime.strptime(candidate, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return {"status": "invalid_timestamp", "max_age_seconds": max_age_seconds}
    age_seconds = (observed_at - exchange_at.astimezone(timezone.utc)).total_seconds()
    result = {"observed_trade_time": exchange_at.isoformat(), "age_seconds": round(age_seconds, 3),
              "max_age_seconds": max_age_seconds}
    if age_seconds < -5:
        return {**result, "status": "future_timestamp"}
    if age_seconds > max_age_seconds:
        return {**result, "status": "stale_timestamp"}
    return {**result, "status": "fresh"}


def quote_from_fuyao(row: dict[str, Any]) -> dict[str, Any] | None:
    """Preserve the Fuyao adapter's normalized all-A row without fake flow."""
    def as_number(value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    symbol = str(row.get("symbol") or "").upper()
    price = as_number(row.get("price"))
    if not _SYMBOL.fullmatch(symbol) or price is None or price <= 0:
        return None
    return {
        "symbol": symbol, "name": row.get("name"), "price": price,
        "pct_change": as_number(row.get("pct_change")), "turnover": as_number(row.get("turnover")),
        "volume": as_number(row.get("volume")), "raw": dict(row.get("raw") or row),
        "price_source": "fuyao_ths_all_a_snapshot", "price_observed_from_depth": False,
        "price_observed_at": row.get("price_observed_at"),
    }


def annotate_flow_percentiles(quotes: dict[str, dict[str, Any]]) -> None:
    """Attach a same-snapshot main-flow percentile without assuming units."""
    ranked = sorted((quote for quote in quotes.values() if quote.get("main_net_inflow") is not None),
                    key=lambda quote: float(quote["main_net_inflow"]))
    denominator = max(1, len(ranked) - 1)
    for index, quote in enumerate(ranked):
        quote["main_flow_percentile"] = round(index / denominator, 5)
        quote["main_flow_rank"] = index + 1
        quote["main_flow_universe"] = len(ranked)


__all__ = [
    "LONGHU_WATCH_QUOTE_VOLUME_UNIT", "annotate_flow_percentiles", "exchange_time_status",
    "merge_eastmoney_watch_flows", "merge_longhu_watch_quotes", "merge_sina_watch_quotes",
    "merge_watch_quote_prices", "observation_source", "quote_from_fuyao",
]

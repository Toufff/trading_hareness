"""Async Longhu market-data reads for intraday capture, study and review.

Every call goes through ``intraday_source()``: the owner's licensed adapter
locally, or the authenticated shared gateway on a peer.  Rows keep the field
contracts the capture/feature code already consumes (board lots, CNY minute
amounts, thousand-CNY daily amounts) and are labelled with Longhu source names.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .longhu_vendor_source import configured, fetch_daily_kline, intraday_source, longhu_security_id

PROVIDER_KEY = "longhuvip"
MINUTE_SOURCE = "longhuvip:GetStockTrendIncremental"
ORDER_BOOK_SOURCE = "longhuvip:GetStockPanKou"
DAILY_SOURCE = "longhuvip:GetKLineDay_W14"
#: Strategy indexes whose daily bars this adapter may serve as the secondary source.
STRATEGY_INDEXES = frozenset({"000001.SH", "000300.SH", "399001.SZ", "399006.SZ"})


class LonghuMarketDataError(RuntimeError):
    pass


async def _blocking(function: Any, *args: Any, timeout_seconds: float) -> Any:
    if not configured():
        raise LonghuMarketDataError("longhu_not_configured")
    return await asyncio.wait_for(asyncio.to_thread(function, *args), timeout=timeout_seconds)


async def longhu_intraday_minute_session(symbol: str) -> dict[str, Any]:
    """Return the latest Longhu minute tape together with the session date it declares.

    The endpoint serves only its latest session.  A caller asking for a named
    trading day must compare ``session_date`` instead of assuming "today".
    """
    rows = await _blocking(lambda: intraday_source().stock_minutes(symbol), timeout_seconds=15)
    dates = {row.get("session_date") for row in rows}
    if len(dates) != 1:
        raise LonghuMarketDataError(f"Longhu minute tape for {symbol} declares {len(dates)} session dates")
    return {"session_date": next(iter(dates)), "rows": rows}


async def longhu_intraday_minutes(symbol: str) -> list[dict[str, Any]]:
    """Return today's Longhu minute tape; the last row is labelled in-progress."""
    return (await longhu_intraday_minute_session(symbol))["rows"]


def order_book_row(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Project a normalized ``GetStockPanKou`` snapshot onto the order-book capture contract."""
    bids, asks = snapshot.get("bids") or [], snapshot.get("asks") or []
    price, pre_close = snapshot.get("price"), snapshot.get("pre_close")
    if len(bids) != 5 or len(asks) != 5 or not price or not pre_close or price <= 0 or pre_close <= 0:
        return None
    valid_bids = [row for row in bids if row["price"] > 0 and row["size"] >= 0]
    valid_asks = [row for row in asks if row["price"] > 0 and row["size"] >= 0]
    if not valid_bids and not valid_asks:
        return None
    return {
        "ts_code": snapshot["ts_code"], "name": snapshot.get("name"), "price": price, "pre_close": pre_close,
        "cumulative_volume_lot": snapshot.get("volume"), "cumulative_amount": snapshot.get("amount"),
        "outer_volume_lot": snapshot.get("outer_volume_lot"), "inner_volume_lot": snapshot.get("inner_volume_lot"),
        "bids": bids, "asks": asks,
        # Preserve a sealed limit book: it is the observation seal/erosion research needs.
        "one_sided_book": bool(valid_bids) != bool(valid_asks),
        "book_side": "bid_only" if valid_bids and not valid_asks else "ask_only" if valid_asks and not valid_bids else "two_sided",
        "seal_volume_lot": (valid_bids[0]["size"] if valid_bids and not valid_asks else
                            valid_asks[0]["size"] if valid_asks and not valid_bids else None),
        "trade_time": snapshot.get("trade_time"), "source": ORDER_BOOK_SOURCE,
    }


async def longhu_order_book_quotes(symbols: list[str], *, max_symbols: int | None = None) -> list[dict[str, Any]]:
    """Fetch one Longhu depth snapshot per explicit watchlist symbol."""
    normalized = list(dict.fromkeys(str(symbol).upper() for symbol in symbols
                                    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(symbol).upper())))
    if not normalized:
        return []
    if max_symbols is not None:
        normalized = normalized[:max(1, int(max_symbols))]
    snapshots, _status = await _blocking(lambda: intraday_source().watch_quotes(normalized), timeout_seconds=20)
    return [row for snapshot in snapshots if (row := order_book_row(snapshot)) is not None]


def _index_quote(symbol: str) -> dict[str, Any] | None:
    """Latest index level from the minute tape (``GetStockPanKou`` does not serve indexes)."""
    resolved = longhu_security_id(symbol)
    if not resolved:
        return None
    envelope = intraday_source().raw_call({
        "target": "longhu_quote",
        "params": {"a": "GetStockTrendIncremental", "c": "StockL2Data", "apiv": "w41", "Type": 1, "StockID": resolved[1]},
    })
    payload = ((envelope.get("pages") or [{}])[0] or {}).get("payload") or {}
    trend = [row for row in payload.get("trend") or [] if isinstance(row, list) and len(row) >= 2]
    try:
        price, pre_close = float(trend[-1][1]), float(payload.get("preclose_px"))
    except (IndexError, TypeError, ValueError):
        return None
    if price <= 0 or pre_close <= 0:
        return None
    return {"ts_code": resolved[0], "price": price, "pre_close": pre_close, "trade_date": str(payload.get("day") or ""),
            "minute": str(trend[-1][0]), "source": MINUTE_SOURCE}


async def longhu_index_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Latest level per index; a failed symbol is absent, never guessed."""
    async def one(symbol: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return symbol, await _blocking(lambda: _index_quote(symbol), timeout_seconds=10)
        except Exception:  # noqa: BLE001 - one index must not drop the others
            return symbol, None

    return {symbol: row for symbol, row in await asyncio.gather(*(one(s) for s in symbols)) if row}


async def longhu_daily(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    """Unadjusted daily stock bars in ``[start, end]`` (YYYYMMDD), lots / thousand CNY."""
    if not longhu_security_id(symbol):
        raise ValueError(f"unsupported Longhu security: {symbol}")
    return await _blocking(lambda: fetch_daily_kline(intraday_source(), symbol, start, end), timeout_seconds=30)


async def longhu_index_daily(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    """Unadjusted daily bars for the bounded strategy index set."""
    if symbol not in STRATEGY_INDEXES:
        raise ValueError("Longhu index daily only accepts the strategy index set")
    rows = await longhu_daily(symbol, start, end)
    return [{**row, "price_basis": "unadjusted_index"} for row in rows]


__all__ = [
    "DAILY_SOURCE", "MINUTE_SOURCE", "ORDER_BOOK_SOURCE", "PROVIDER_KEY", "STRATEGY_INDEXES",
    "LonghuMarketDataError", "longhu_daily", "longhu_index_daily", "longhu_index_quotes", "longhu_intraday_minute_session",
    "longhu_intraday_minutes", "longhu_order_book_quotes", "order_book_row",
]

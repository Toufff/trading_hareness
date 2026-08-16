"""Point-in-time daily factors for the explicit intraday observation pool."""

from __future__ import annotations

from statistics import mean
from collections import defaultdict
from typing import Any, Callable, Iterable

from .post_close_structures import daily_base_structure
from .research_prices import adjusted_bars


def daily_factors_from_rows(rows: Iterable[dict[str, Any]], *, number: Callable[[Any], float | None]) -> dict[str, Any]:
    """Compute one symbol's bounded adjusted factors from already-loaded bars."""
    bars = list(rows)
    research_bars, adjustment_flags = adjusted_bars(bars)
    closes = [number(row.get("research_close")) for row in research_bars] if research_bars is not None else []
    volumes = [number(row.get("volume")) for row in bars]
    trade_constraints = ({"is_suspended": bool(bars[-1].get("is_suspended")), "is_st": bool(bars[-1].get("is_st")),
                          "limit_up": bars[-1].get("limit_up"), "limit_down": bars[-1].get("limit_down")} if bars else {})
    if research_bars is None:
        return {"status": "data_quality_blocked", "bar_count": len(bars), "quality_flags": adjustment_flags,
                "trade_constraints": trade_constraints}
    if len(closes) < 21 or any(value is None for value in closes):
        return {"status": "insufficient_history", "bar_count": len(bars), "quality_flags": adjustment_flags,
                "trade_constraints": trade_constraints}
    close_values = [float(value) for value in closes if value is not None]
    sma5, sma20 = mean(close_values[-5:]), mean(close_values[-20:])
    gains, losses = [], []
    for current, previous in zip(close_values[-15:], close_values[-16:-1], strict=True):
        change = current - previous
        gains.append(max(0.0, change)); losses.append(max(0.0, -change))
    avg_gain, avg_loss = mean(gains), mean(losses)
    rsi14 = 100.0 if avg_loss == 0 and avg_gain else 0.0 if avg_gain == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    returns = [close_values[index] / close_values[index - 1] - 1 for index in range(-20, 0) if close_values[index - 1]]
    volatility20 = (mean([value * value for value in returns]) ** 0.5) if returns else None
    valid_volumes = [float(value) for value in volumes[-20:] if value is not None]
    volume_ratio20 = (float(volumes[-1]) / mean(valid_volumes)
                      if volumes and volumes[-1] is not None and valid_volumes and mean(valid_volumes) else None)
    base_structure = daily_base_structure(bars)
    return {"status": "completed", "bar_count": len(bars), "latest_daily_close": close_values[-1],
            "sma5": round(sma5, 4), "sma20": round(sma20, 4),
            "ma_trend": "bullish" if close_values[-1] > sma5 > sma20 else "bearish" if close_values[-1] < sma5 < sma20 else "mixed",
            "rsi14": round(rsi14, 2), "volatility20": round(volatility20, 5) if volatility20 is not None else None,
            "volume_ratio20": round(volume_ratio20, 3) if volume_ratio20 is not None else None,
            "base_structure": base_structure, "base_structure_ready": base_structure.get("status") == "ready",
            "quality_flags": adjustment_flags, "trade_constraints": trade_constraints}


def watchlist_daily_factors(symbol: str, connection: Any, *, number: Callable[[Any], float | None]) -> dict[str, Any]:
    """Compute bounded adjusted factors from a caller-owned transaction."""
    rows = connection.execute(
        """SELECT b.trading_date,b.high,b.low,b.close,b.volume,b.adj_factor,b.is_suspended,b.limit_up,b.limit_down,
                  i.is_st
             FROM quant.canonical_bars_daily b
             JOIN quant.instruments i ON i.symbol=b.symbol
             WHERE b.symbol=%s ORDER BY b.trading_date DESC LIMIT 61""", (symbol,)
    ).fetchall()
    return daily_factors_from_rows((dict(row) for row in reversed(rows)), number=number)


def watchlist_daily_factors_by_symbol(
    symbols: Iterable[str], connection: Any, *, number: Callable[[Any], float | None],
) -> dict[str, dict[str, Any]]:
    """Compute factors for an explicit watch basket with one bounded SQL read.

    The live scanner already owns a single transaction.  Pulling 61 bars per
    symbol through a ranked query preserves the single-symbol factor semantics
    while avoiding a query per watch on every 10/30-second scan.
    """
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    if not requested:
        return {}
    rows = connection.execute(
        """WITH ranked AS (
               SELECT b.symbol,b.trading_date,b.high,b.low,b.close,b.volume,b.adj_factor,b.is_suspended,b.limit_up,b.limit_down,
                      i.is_st,row_number() OVER(PARTITION BY b.symbol ORDER BY b.trading_date DESC) AS row_number
                 FROM quant.canonical_bars_daily b
                 JOIN quant.instruments i ON i.symbol=b.symbol
                WHERE b.symbol=ANY(%s)
           )
           SELECT symbol,trading_date,high,low,close,volume,adj_factor,is_suspended,limit_up,limit_down,is_st
             FROM ranked WHERE row_number<=61 ORDER BY symbol,trading_date ASC""",
        (requested,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = dict(row)
        grouped[str(payload.pop("symbol"))].append(payload)
    return {
        symbol: daily_factors_from_rows(grouped.get(symbol, ()), number=number)
        for symbol in requested
    }


__all__ = ["daily_factors_from_rows", "watchlist_daily_factors", "watchlist_daily_factors_by_symbol"]

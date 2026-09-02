"""Point-in-time daily factors for the explicit intraday observation pool."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from collections import defaultdict
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .post_close_structures import daily_base_structure
from .research_prices import adjusted_bars
from .technical_analysis import rsi as technical_rsi


def daily_factors_from_rows(
    rows: Iterable[dict[str, Any]], *, number: Callable[[Any], float | None],
    current_limit_up: Any = None, current_limit_down: Any = None,
) -> dict[str, Any]:
    """Compute one symbol's bounded adjusted factors from already-loaded bars.

    ``rows`` is expected to already be point-in-time bounded by the caller
    (strictly before the observation date, and not carrying an
    ``available_at`` later than the observation instant), so every bar here
    is treated as a settled prior session.  ``current_limit_up``/
    ``current_limit_down`` -- the *current* session's own published band,
    which is known before the open and safe to read for "today" -- come from
    ``quant.daily_trade_limits`` rather than from a same-day row in this bar
    set, since today's own close is exactly what a bounded read must exclude.
    """
    bars = list(rows)
    research_bars, adjustment_flags = adjusted_bars(bars)
    closes = [number(row.get("research_close")) for row in research_bars] if research_bars is not None else []
    volumes = [number(row.get("volume")) for row in bars]
    trade_constraints = ({
        "is_suspended": bool(bars[-1].get("is_suspended")), "is_st": bool(bars[-1].get("is_st")),
        "limit_up": current_limit_up if current_limit_up is not None else bars[-1].get("limit_up"),
        "limit_down": current_limit_down if current_limit_down is not None else bars[-1].get("limit_down"),
    } if bars else {})
    if research_bars is None:
        return {"status": "data_quality_blocked", "bar_count": len(bars), "quality_flags": adjustment_flags,
                "trade_constraints": trade_constraints}
    if len(closes) < 21 or any(value is None for value in closes):
        return {"status": "insufficient_history", "bar_count": len(bars), "quality_flags": adjustment_flags,
                "trade_constraints": trade_constraints}
    close_values = [float(value) for value in closes if value is not None]
    sma5, sma20 = mean(close_values[-5:]), mean(close_values[-20:])
    # The shared technical_analysis implementation is the single source of
    # truth for RSI (a true 14-period Wilder RSI needs 15 closes: 14 changes
    # plus the anchor); this module's own inline computation used a 15-close/
    # 14-change window and was actually a 15-period RSI mislabelled "rsi14".
    rsi14 = technical_rsi(close_values, period=14)
    returns = [close_values[index] / close_values[index - 1] - 1 for index in range(-20, 0) if close_values[index - 1]]
    volatility20 = (mean([value * value for value in returns]) ** 0.5) if returns else None
    # The volume-ratio baseline excludes today's own volume: comparing a
    # session's volume against a mean that already contains itself
    # systematically compresses the ratio toward 1.0.
    baseline_volumes = [float(value) for value in volumes[-21:-1] if value is not None]
    volume_ratio20 = (float(volumes[-1]) / mean(baseline_volumes)
                      if volumes and volumes[-1] is not None and baseline_volumes and mean(baseline_volumes) else None)
    base_structure = daily_base_structure(bars)
    return {"status": "completed", "bar_count": len(bars), "latest_daily_close": close_values[-1],
            "sma5": round(sma5, 4), "sma20": round(sma20, 4),
            "ma_trend": "bullish" if close_values[-1] > sma5 > sma20 else "bearish" if close_values[-1] < sma5 < sma20 else "mixed",
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "volatility20": round(volatility20, 5) if volatility20 is not None else None,
            "volume_ratio20": round(volume_ratio20, 3) if volume_ratio20 is not None else None,
            "base_structure": base_structure, "base_structure_ready": base_structure.get("status") == "ready",
            "quality_flags": adjustment_flags, "trade_constraints": trade_constraints}


def watchlist_daily_factors(
    symbol: str, connection: Any, *, number: Callable[[Any], float | None], observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Compute bounded adjusted factors from a caller-owned transaction.

    Only bars strictly before the observation date, and not carrying an
    ``available_at`` later than the observation instant, are read: a same-day
    row landed by a public source mid-session is a still-forming bar, not a
    settled close, and the read must not silently use its own future.
    ``observed_at`` defaults to wall-clock now, which is the correct
    observation instant for every live-scan call site today.  Today's own
    published price-limit band is joined from ``quant.daily_trade_limits`` in
    the same query (rather than a second round trip) so this keeps the
    caller's existing single-query-per-symbol contract inside a shared
    transaction.
    """
    observed_at = observed_at or datetime.now(timezone.utc)
    observed_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    rows = connection.execute(
        """WITH bars AS (
               SELECT b.trading_date,b.high,b.low,b.close,b.volume,b.adj_factor,b.is_suspended,b.limit_up,b.limit_down,
                      i.is_st
                 FROM quant.canonical_bars_daily b
                 JOIN quant.instruments i ON i.symbol=b.symbol
                WHERE b.symbol=%s AND b.trading_date<%s AND b.available_at<=%s
                ORDER BY b.trading_date DESC LIMIT 61
           ), today_limit AS (
               SELECT limit_up,limit_down FROM quant.daily_trade_limits
                WHERE symbol=%s AND trading_date=%s ORDER BY provider LIMIT 1
           )
           SELECT bars.*, today_limit.limit_up AS current_limit_up, today_limit.limit_down AS current_limit_down
             FROM bars LEFT JOIN today_limit ON true""",
        (symbol, observed_date, observed_at, symbol, observed_date),
    ).fetchall()
    current_limit_up = dict(rows[0]).get("current_limit_up") if rows else None
    current_limit_down = dict(rows[0]).get("current_limit_down") if rows else None
    return daily_factors_from_rows(
        (dict(row) for row in reversed(rows)), number=number,
        current_limit_up=current_limit_up, current_limit_down=current_limit_down,
    )


def watchlist_daily_factors_by_symbol(
    symbols: Iterable[str], connection: Any, *, number: Callable[[Any], float | None],
    observed_at: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute factors for an explicit watch basket with one bounded SQL read.

    The live scanner already owns a single transaction.  Pulling 61 bars per
    symbol through a ranked query preserves the single-symbol factor semantics
    while avoiding a query per watch on every 10/30-second scan.  As in
    ``watchlist_daily_factors``, only sessions strictly before the observation
    date and already available by the observation instant are read, and
    today's own published price-limit band is joined in from
    ``quant.daily_trade_limits`` in the same one query.
    """
    requested = sorted({str(symbol) for symbol in symbols if str(symbol)})
    if not requested:
        return {}
    observed_at = observed_at or datetime.now(timezone.utc)
    observed_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    rows = connection.execute(
        """WITH ranked AS (
               SELECT b.symbol,b.trading_date,b.high,b.low,b.close,b.volume,b.adj_factor,b.is_suspended,b.limit_up,b.limit_down,
                      i.is_st,row_number() OVER(PARTITION BY b.symbol ORDER BY b.trading_date DESC) AS row_number
                 FROM quant.canonical_bars_daily b
                 JOIN quant.instruments i ON i.symbol=b.symbol
                WHERE b.symbol=ANY(%s) AND b.trading_date<%s AND b.available_at<=%s
           ), current_limits AS (
               SELECT DISTINCT ON (symbol) symbol,limit_up AS current_limit_up,limit_down AS current_limit_down
                 FROM quant.daily_trade_limits
                WHERE symbol=ANY(%s) AND trading_date=%s
                ORDER BY symbol,provider
           )
           SELECT ranked.symbol,ranked.trading_date,ranked.high,ranked.low,ranked.close,ranked.volume,ranked.adj_factor,
                  ranked.is_suspended,ranked.limit_up,ranked.limit_down,ranked.is_st,
                  current_limits.current_limit_up,current_limits.current_limit_down
             FROM ranked LEFT JOIN current_limits ON current_limits.symbol=ranked.symbol
            WHERE ranked.row_number<=61 ORDER BY ranked.symbol,ranked.trading_date ASC""",
        (requested, observed_date, observed_at, requested, observed_date),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_limits: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        symbol = str(payload.pop("symbol"))
        current_limit_up = payload.pop("current_limit_up", None)
        current_limit_down = payload.pop("current_limit_down", None)
        current_limits.setdefault(symbol, {"limit_up": current_limit_up, "limit_down": current_limit_down})
        grouped[symbol].append(payload)
    return {
        symbol: daily_factors_from_rows(
            grouped.get(symbol, ()), number=number,
            current_limit_up=current_limits.get(symbol, {}).get("limit_up"),
            current_limit_down=current_limits.get(symbol, {}).get("limit_down"),
        )
        for symbol in requested
    }


__all__ = ["daily_factors_from_rows", "watchlist_daily_factors", "watchlist_daily_factors_by_symbol"]

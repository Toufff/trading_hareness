"""Pure daily features for limit-up and reversal review."""

from __future__ import annotations

from statistics import mean
from typing import Any, Callable


def limit_daily_features(bars: list[dict[str, Any]], *, number: Callable[[Any], float | None],
                         limit_ratio: Callable[[str, bool], float]) -> dict[str, Any]:
    """Describe a selected limit-up session against only earlier daily bars."""
    if not bars:
        return {"status": "missing_daily_bar"}
    ordered = sorted(bars, key=lambda item: str(item.get("trading_date") or ""))
    current = ordered[-1]
    previous_close = number(current.get("pre_close"))
    if previous_close is None and len(ordered) >= 2:
        previous_close = number(ordered[-2].get("close"))
    opened, high, low, close = (number(current.get(key)) for key in ("open", "high", "low", "close"))
    exact_limit_up, exact_limit_down = number(current.get("limit_up")), number(current.get("limit_down"))
    ratio = limit_ratio(str(current.get("symbol") or ""), bool(current.get("is_st")))
    implied_limit_pct = round((exact_limit_up / previous_close - 1) * 100, 4) if exact_limit_up and previous_close else ratio * 100
    volume = number(current.get("volume"))
    prior_volumes = [number(row.get("volume")) for row in ordered[:-1]]
    valid_prior_5 = [float(value) for value in prior_volumes[-5:] if value is not None and value > 0]
    valid_prior_20 = [float(value) for value in prior_volumes[-20:] if value is not None and value > 0]
    pct = lambda value: round((value / previous_close - 1) * 100, 4) if value is not None and previous_close and previous_close > 0 else None
    close_location = ((close - low) / (high - low)) if close is not None and low is not None and high is not None and high > low else None
    low_pct, high_pct, close_pct = pct(low), pct(high), pct(close)
    deep_reversal = bool(low_pct is not None and low_pct <= -implied_limit_pct * 0.85
                         and close_pct is not None and close_pct >= implied_limit_pct * 0.95)
    return {
        "status": "completed", "trading_date": str(current.get("trading_date")),
        "selected_provider": current.get("selected_provider"), "bar_count": len(ordered),
        "open": opened, "high": high, "low": low, "close": close, "pre_close": previous_close,
        "limit_up": exact_limit_up, "limit_down": exact_limit_down, "limit_pct": implied_limit_pct,
        "open_pct": pct(opened), "high_pct": high_pct, "low_pct": low_pct, "close_pct": close_pct,
        "intraday_range_pct": round((high / low - 1) * 100, 4) if high and low and low > 0 else None,
        "close_location": round(close_location, 4) if close_location is not None else None,
        "volume": volume,
        "volume_multiple_5d": round(volume / mean(valid_prior_5), 4) if volume and valid_prior_5 and mean(valid_prior_5) else None,
        "volume_multiple_20d": round(volume / mean(valid_prior_20), 4) if volume and valid_prior_20 and mean(valid_prior_20) else None,
        "ground_to_sky_daily_shape": deep_reversal,
    }


__all__ = ["limit_daily_features"]

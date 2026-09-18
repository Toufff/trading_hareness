"""Pure daily-bar metrics and holding-stage classification.

Metric definitions deliberately match ``app/short_term_lanes/rules.features``
(ma5/ma10 are simple closing means, ``prior_high`` is the highest of the five
closes before today, ``recent_low`` the lowest of the last five closes) and
``app/stock_workbench_indicators._atr`` so a discipline plan and a lane report
never describe the same structure with two different numbers.

Nothing here reads a database or a provider.  Missing or malformed bars fail
closed with ``InsufficientBars`` instead of being forward filled.
"""

from __future__ import annotations

from datetime import date
from math import isfinite
from statistics import fmean, pstdev
from typing import Any

from ..stock_workbench_indicators import _atr

MIN_BARS = 21
ATR_PERIOD = 14

# Stage thresholds; every branch below has a test.
CRASH_DRAWDOWN_PCT = -25.0
TREND_DRAWDOWN_PCT = -5.0
PULLBACK_DRAWDOWN_BAND_PCT = (-15.0, -3.0)
BASE_RANGE10_PCT = 8.0
BASE_MA20_BAND_PCT = 3.0

STAGES = ("crash_rebound", "broken", "breakout_hold", "trend_hold",
          "pullback_hold", "base_platform", "unclassified")


class InsufficientBars(ValueError):
    """Raised when the bar history cannot support the frozen metric snapshot."""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _bar_date(row: dict[str, Any]) -> str:
    raw = row.get("trading_date") or row.get("trade_date") or row.get("date")
    if isinstance(raw, date):
        return raw.isoformat()
    text = str(raw or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def normalize_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only bars whose OHLC is internally consistent, ordered by date."""
    cleaned: list[dict[str, Any]] = []
    for row in bars:
        day = _bar_date(row)
        values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close")}
        if not day or any(value is None or value <= 0 for value in values.values()):
            continue
        low, high, open_, close = values["low"], values["high"], values["open"], values["close"]
        assert low is not None and high is not None and open_ is not None and close is not None
        if low > high or not low <= open_ <= high or not low <= close <= high:
            continue
        cleaned.append({
            "trading_date": day, **values,
            "volume": _number(row.get("volume")), "amount": _number(row.get("amount")),
            "synthetic": bool(row.get("synthetic", False)),
        })
    cleaned.sort(key=lambda item: item["trading_date"])
    return cleaned


def daily_metrics(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze the indicator snapshot a plan is derived from.

    Returns plain JSON types so the snapshot can be persisted verbatim and a
    later evaluation can recompute every quoted price from it.
    """
    rows = normalize_bars(bars)
    if len(rows) < MIN_BARS:
        raise InsufficientBars(f"need at least {MIN_BARS} valid daily bars, got {len(rows)}")
    closes = [row["close"] for row in rows]
    lows = [row["low"] for row in rows]
    highs = [row["high"] for row in rows]
    volumes = [row["volume"] for row in rows if row["volume"] is not None]
    atr_series = _atr(rows, ATR_PERIOD)
    atr14 = atr_series[-1]
    if atr14 is None or atr14 <= 0:
        raise InsufficientBars("atr14 is unavailable for the latest bar")
    window20 = closes[-20:]
    window10 = closes[-10:]
    returns = [(closes[index] / closes[index - 1] - 1) * 100 for index in range(len(closes) - 10, len(closes))]
    hi20 = max(window20)
    latest = rows[-1]
    prior_volume_mean = fmean(volumes[-6:-1]) if len(volumes) >= 6 else None
    return {
        "trading_date": latest["trading_date"],
        "bars_used": len(rows),
        "synthetic_bars": sum(1 for row in rows if row["synthetic"]),
        "open": latest["open"], "high": latest["high"], "low": latest["low"], "close": latest["close"],
        "prev_close": closes[-2], "prev_low": lows[-2], "prev_high": highs[-2],
        "ma5": fmean(closes[-5:]), "ma10": fmean(closes[-10:]), "ma20": fmean(window20),
        "atr14": float(atr14),
        "hi20": hi20, "lo20": min(window20),
        # the real crash low: the lowest *low* of the last 20 sessions (``lo20`` is the lowest close)
        "low20": min(lows[-20:]),
        "prior_high": max(closes[-6:-1]), "prior_high_prev": max(closes[-7:-2]),
        "recent_low": min(closes[-5:]),
        "low10_close": min(window10), "high10_close": max(window10),
        "low3": min(lows[-3:]),
        "drawdown_pct": (closes[-1] / hi20 - 1) * 100,
        "range10_pct": (max(window10) / min(window10) - 1) * 100,
        "volatility": pstdev(returns),
        "volume": latest["volume"], "amount": latest["amount"],
        "prior_volume_mean5": prior_volume_mean,
        "max_volume20": max(volumes[-20:]) if volumes else None,
    }


def classify_stage(metrics: dict[str, Any], lane: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return ``{stage, rule_id, reason, evidence, lane}``; first matching branch wins.

    The lane attribution from the latest post-close/intraday scan is recorded as
    context only.  It tunes the template wording, it never overrides the branch
    the daily data selected.
    """
    close = float(metrics["close"])
    ma5, ma10, ma20 = float(metrics["ma5"]), float(metrics["ma10"]), float(metrics["ma20"])
    drawdown = float(metrics["drawdown_pct"])
    prior_high, prev_close = float(metrics["prior_high"]), float(metrics["prev_close"])
    range10 = float(metrics["range10_pct"])
    lane_key = (lane or {}).get("lane")
    evidence = {
        "close": close, "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "drawdown_pct": drawdown, "prior_high": prior_high, "range10_pct": range10,
        "lane": lane_key, "formal_state": (lane or {}).get("formal_state"),
    }

    def decided(stage: str, rule_id: str, reason: str) -> dict[str, Any]:
        return {"stage": stage, "rule_id": rule_id, "reason": reason, "evidence": evidence, "lane": lane_key}

    if drawdown <= CRASH_DRAWDOWN_PCT and (ma5 < ma10 or close < ma10):
        return decided("crash_rebound", "stage.crash_rebound",
                       f"距20日最高回撤{drawdown:.1f}%（≤{CRASH_DRAWDOWN_PCT:.0f}%）且短均线未修复")
    if close < ma20 and ma5 < ma10:
        return decided("broken", "stage.broken",
                       f"收盘{close:.2f}低于MA20{ma20:.2f}且MA5{ma5:.2f}<MA10{ma10:.2f}")
    # ``prior_high`` is the highest of the five closes before today; yesterday's
    # own platform is the highest of the five closes before yesterday.
    breakout_today = close > prior_high
    breakout_yesterday = prev_close > float(metrics["prior_high_prev"])
    if (breakout_today or breakout_yesterday) and close >= ma5 >= ma10:
        return decided("breakout_hold", "stage.breakout_hold",
                       f"收盘越过前5日收盘平台{prior_high:.2f}且站上MA5{ma5:.2f}≥MA10{ma10:.2f}")
    if ma5 >= ma10 >= ma20 and drawdown > TREND_DRAWDOWN_PCT:
        return decided("trend_hold", "stage.trend_hold",
                       f"MA5≥MA10≥MA20且回撤{drawdown:.1f}%浅于{TREND_DRAWDOWN_PCT:.0f}%")
    low_band, high_band = PULLBACK_DRAWDOWN_BAND_PCT
    if ma10 >= ma20 and low_band <= drawdown <= high_band and close >= ma10:
        return decided("pullback_hold", "stage.pullback_hold",
                       f"MA10≥MA20，回撤{drawdown:.1f}%在[{low_band:.0f}%,{high_band:.0f}%]且收盘仍在MA10之上")
    if range10 <= BASE_RANGE10_PCT and abs(close / ma20 - 1) * 100 <= BASE_MA20_BAND_PCT:
        return decided("base_platform", "stage.base_platform",
                       f"10日收盘区间{range10:.1f}%≤{BASE_RANGE10_PCT:.0f}%且收盘贴近MA20{ma20:.2f}")
    return decided("unclassified", "stage.unclassified", "未命中任何已定义阶段，使用最保守模板")


__all__ = [
    "ATR_PERIOD", "BASE_MA20_BAND_PCT", "BASE_RANGE10_PCT", "CRASH_DRAWDOWN_PCT", "InsufficientBars",
    "MIN_BARS", "PULLBACK_DRAWDOWN_BAND_PCT", "STAGES", "TREND_DRAWDOWN_PCT", "classify_stage",
    "daily_metrics", "normalize_bars",
]

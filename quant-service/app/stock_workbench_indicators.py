"""Strict, side-effect-free indicators for the interactive stock workbench."""

from __future__ import annotations

from math import isfinite, sqrt
from statistics import fmean, pstdev
from typing import Any


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _rolling_mean(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        window = values[max(0, index - period + 1):index + 1]
        result.append(fmean(window) if len(window) == period else None)
    return result


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _wilder_rsi(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gain = fmean(max(change, 0) for change in changes[:period])
    loss = fmean(max(-change, 0) for change in changes[:period])

    def value() -> float:
        if gain == 0 and loss == 0:
            return 50.0
        if loss == 0:
            return 100.0
        return 100 - 100 / (1 + gain / loss)

    result[period] = value()
    for index in range(period + 1, len(values)):
        change = changes[index - 1]
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
        result[index] = value()
    return result


def _atr(rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    true_ranges: list[float] = []
    for index, row in enumerate(rows):
        high, low = float(row["high"]), float(row["low"])
        if index == 0:
            true_ranges.append(high - low)
        else:
            prior_close = float(rows[index - 1]["close"])
            true_ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    return _rolling_mean(true_ranges, period)


def _kdj(rows: list[dict[str, Any]], period: int = 9) -> tuple[list[float | None], list[float | None], list[float | None]]:
    k_values: list[float | None] = []
    d_values: list[float | None] = []
    j_values: list[float | None] = []
    k = d = 50.0
    for index, row in enumerate(rows):
        window = rows[max(0, index - period + 1):index + 1]
        if len(window) < period:
            k_values.append(None); d_values.append(None); j_values.append(None)
            continue
        low = min(float(item["low"]) for item in window)
        high = max(float(item["high"]) for item in window)
        rsv = 50.0 if high == low else (float(row["close"]) - low) / (high - low) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        k_values.append(k); d_values.append(d); j_values.append(3 * k - 2 * d)
    return k_values, d_values, j_values


def enrich_daily_bars(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate indicators only when every OHLC value is real.

    Invalid rows are reported as gaps and never repaired by copying ``close``
    into open/high/low.  The frontend therefore cannot draw a fabricated bar.
    """
    ordered = sorted(rows, key=lambda row: str(row.get("date") or row.get("trading_date") or ""))
    valid: list[dict[str, Any]] = []
    gaps: list[str] = []
    for row in ordered:
        date = str(row.get("date") or row.get("trading_date") or "")
        values = {key: number(row.get(key)) for key in ("open", "high", "low", "close")}
        if not date or any(value is None for value in values.values()):
            gaps.append(date or "unknown")
            continue
        assert all(value is not None for value in values.values())
        if values["low"] > values["high"] or not values["low"] <= values["open"] <= values["high"] or not values["low"] <= values["close"] <= values["high"]:
            gaps.append(date)
            continue
        valid.append({**row, **values, "date": date})
    if not valid:
        return {"status": "unavailable", "bars": [], "gaps": gaps, "summary": {}}

    closes = [float(row["close"]) for row in valid]
    ma5, ma10, ma20, ma60 = (_rolling_mean(closes, period) for period in (5, 10, 20, 60))
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif = [fast - slow for fast, slow in zip(ema12, ema26, strict=True)]
    dea = _ema(dif, 9)
    macd = [(d - e) * 2 for d, e in zip(dif, dea, strict=True)]
    rsi14 = _wilder_rsi(closes)
    atr14 = _atr(valid)
    k_values, d_values, j_values = _kdj(valid)
    enriched: list[dict[str, Any]] = []
    for index, row in enumerate(valid):
        boll_mid = ma20[index]
        window = closes[max(0, index - 19):index + 1]
        deviation = pstdev(window) if len(window) == 20 else None
        enriched.append({
            **row,
            "volume": number(row.get("volume")), "amount": number(row.get("amount")),
            "turnover_rate": number(row.get("turnover_rate")), "volume_ratio": number(row.get("volume_ratio")),
            "ma5": ma5[index], "ma10": ma10[index], "ma20": ma20[index], "ma60": ma60[index],
            "dif": dif[index], "dea": dea[index], "macd": macd[index], "rsi14": rsi14[index],
            "k": k_values[index], "d": d_values[index], "j": j_values[index], "atr14": atr14[index],
            "boll_mid": boll_mid,
            "boll_upper": boll_mid + 2 * deviation if boll_mid is not None and deviation is not None else None,
            "boll_lower": boll_mid - 2 * deviation if boll_mid is not None and deviation is not None else None,
        })

    latest = enriched[-1]
    prior_amounts = [float(item["amount"]) for item in enriched[-6:-1] if number(item.get("amount")) is not None]
    amount_multiple = (float(latest["amount"]) / fmean(prior_amounts)
                       if number(latest.get("amount")) is not None and len(prior_amounts) == 5 and fmean(prior_amounts) > 0 else None)
    recent5, recent10 = enriched[-5:], enriched[-10:]
    support = min(float(row["low"]) for row in recent5)
    resistance = max(float(row["high"]) for row in recent10[:-1] or recent10)
    returns = [(closes[index] / closes[index - 1] - 1) * 100 for index in range(1, len(closes))]
    summary = {
        "close": closes[-1], "ma5": ma5[-1], "ma10": ma10[-1], "ma20": ma20[-1], "ma60": ma60[-1],
        "rsi14": rsi14[-1], "atr14": atr14[-1], "amount_multiple_5": amount_multiple,
        "support_5": support, "resistance_10": resistance,
        "return_5d_pct": (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else None,
        "return_20d_pct": (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None,
        "realized_volatility_20_pct": pstdev(returns[-20:]) * sqrt(20) if len(returns) >= 20 else None,
    }
    status = "ready" if len(enriched) >= 21 and not gaps else "partial" if enriched else "unavailable"
    return {"status": status, "bars": enriched, "gaps": gaps, "summary": summary}


def weekly_bars(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate strict daily rows into exchange-week OHLC bars."""
    from datetime import date

    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        stamp = date.fromisoformat(str(row["date"])[:10])
        iso = stamp.isocalendar()
        groups.setdefault((iso.year, iso.week), []).append(row)
    result: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        result.append({
            "date": group[-1]["date"], "open": group[0]["open"], "high": max(row["high"] for row in group),
            "low": min(row["low"] for row in group), "close": group[-1]["close"],
            "volume": sum(number(row.get("volume")) or 0 for row in group),
            "amount": sum(number(row.get("amount")) or 0 for row in group),
        })
    return result


__all__ = ["enrich_daily_bars", "number", "weekly_bars"]

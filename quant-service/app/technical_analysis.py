"""Deterministic, side-effect-free technical summary used by stock studies."""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date_key(row: dict[str, Any]) -> str:
    return str(row.get("trade_date") or row.get("date") or "")


def technical_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted((row for row in rows if _number(row.get("close")) is not None), key=_date_key)
    closes = [value for row in ordered if (value := _number(row.get("close"))) is not None]
    if not closes:
        return {"status": "insufficient_market_data", "score": None, "trend": "unknown", "reasons": ["没有可计算的日线收盘价"]}

    def average(window: int) -> float | None:
        return round(sum(closes[-window:]) / window, 4) if len(closes) >= window else None

    def change(days: int) -> float | None:
        return round((closes[-1] / closes[-days - 1] - 1) * 100, 2) if len(closes) > days else None

    sma5, sma10, sma20 = average(5), average(10), average(20)
    gains = [max(closes[index] - closes[index - 1], 0) for index in range(max(1, len(closes) - 14), len(closes))]
    losses = [max(closes[index - 1] - closes[index], 0) for index in range(max(1, len(closes) - 14), len(closes))]
    rsi14 = None
    if len(gains) == 14:
        average_loss = sum(losses) / 14
        rsi14 = 100.0 if average_loss == 0 else round(100 - 100 / (1 + (sum(gains) / 14) / average_loss), 2)
    score, reasons = 50, []
    if sma20 is not None:
        if closes[-1] > sma20:
            score += 15; reasons.append("收盘价高于 20 日均线")
        else:
            score -= 15; reasons.append("收盘价低于 20 日均线")
    if sma5 is not None and sma20 is not None:
        if sma5 > sma20:
            score += 10; reasons.append("5 日均线高于 20 日均线")
        else:
            score -= 10; reasons.append("5 日均线低于 20 日均线")
    return_5d = change(5)
    if return_5d is not None:
        score += 8 if return_5d > 0 else -8 if return_5d < 0 else 0
        reasons.append(f"5 日涨跌幅 {return_5d}%")
    if rsi14 is not None and rsi14 >= 70:
        score -= 5; reasons.append("RSI(14) 处于偏高区间")
    elif rsi14 is not None and rsi14 <= 30:
        score += 5; reasons.append("RSI(14) 处于偏低区间")
    trend = "positive" if score >= 62 else "negative" if score <= 38 else "neutral"
    return {
        "status": "ready", "as_of_date": _date_key(ordered[-1]), "close": closes[-1], "score": max(0, min(100, score)),
        "trend": trend, "return_1d_pct": change(1), "return_5d_pct": return_5d, "return_20d_pct": change(20),
        "sma_5": sma5, "sma_10": sma10, "sma_20": sma20, "rsi_14": rsi14, "reasons": reasons,
    }

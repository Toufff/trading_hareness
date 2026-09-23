"""Causal close-only intraday features for manually focused holdings.

Longhu's trend tape has a per-minute close, volume and amount, not true minute
OHLC.  KDJ, ATR and ADX are intentionally absent from this projection.
"""

from __future__ import annotations

from statistics import mean, median, pstdev
from typing import Any

from ..trade_discipline.alerts_evaluation import ValidatedMinuteTape

VERSION = 'focus-close-only-v1'


def _ema(values: list[float], period: int) -> list[float]:
    weight = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(weight * value + (1 - weight) * output[-1])
    return output


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 2:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:])]
    gain = mean(max(change, 0) for change in changes[:period])
    loss = mean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    return 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def technical_evidence(tape: ValidatedMinuteTape) -> dict[str, Any]:
    rows = list(tape.rows)
    if len(rows) < 60:
        return {'status': 'insufficient_history', 'version': VERSION,
                'completed_minutes': len(rows), 'as_of': tape.as_of.isoformat()}
    try:
        closes = [float(row['close']) for row in rows]
        volumes = [float(row['volume_lot']) for row in rows]
        amounts = [float(row['amount']) if row.get('amount') is not None else None for row in rows]
    except (TypeError, ValueError, KeyError):
        return {'status': 'invalid_tape', 'version': VERSION, 'as_of': tape.as_of.isoformat()}
    if any(price <= 0 for price in closes) or any(volume < 0 for volume in volumes):
        return {'status': 'invalid_tape', 'version': VERSION, 'as_of': tape.as_of.isoformat()}
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    rsi = _rsi(closes)
    middle = mean(closes[-20:])
    deviation = pstdev(closes[-20:])
    upper, lower = middle + 2 * deviation, middle - 2 * deviation
    amount_recent = amounts[-3:]
    prior = [value for value in amounts[-23:-3] if value is not None and value > 0]
    amount_ratio = sum(amount_recent) / 3 / median(prior) if len(prior) >= 10 and all(
        value is not None and value >= 0 for value in amount_recent) else None
    price = closes[-1]
    change3 = (price / closes[-4] - 1) * 100
    range15 = (max(closes[-15:]) / min(closes[-15:]) - 1) * 100
    vwap = float(rows[-1].get('vwap') or 0)
    vwap_ok = vwap > 0 and abs(price / vwap - 1) <= 0.5
    rising = hist[-1] > hist[-2] > hist[-3]
    falling = hist[-1] < hist[-2] < hist[-3]
    state = 'mixed'
    if (vwap_ok and price > vwap and hist[-1] > 0 and rising and rsi is not None and 45 <= rsi <= 75
            and change3 >= .3 and amount_ratio is not None and amount_ratio >= 1.3):
        state = 'bullish_confirmation'
    elif (vwap_ok and price < vwap and hist[-1] < 0 and falling and rsi is not None and 25 <= rsi <= 55
          and change3 <= -.3 and amount_ratio is not None and amount_ratio >= 1.3):
        state = 'bearish_confirmation'
    elif (range15 <= .55 and amount_ratio is not None and amount_ratio <= .85
          and abs(hist[-1] / price) * 100 < .1):
        state = 'sideways'
    return {'status': 'ready', 'version': VERSION, 'source': 'longhu_minute_close_only',
            'as_of': tape.as_of.isoformat(), 'completed_minutes': len(rows),
            'price': round(price, 3), 'change_3m_pct': round(change3, 3),
            'range_15m_pct': round(range15, 3), 'amount_ratio_3m': round(amount_ratio, 3) if amount_ratio is not None else None,
            'vwap': round(vwap, 3) if vwap_ok else None,
            'macdfs': round(hist[-1], 5), 'macdfs_rising_3m': rising,
            'macdfs_formula': '2*(EMA12(close)-EMA26(close)-EMA9(DIF))',
            'rsi14': round(rsi, 2) if rsi is not None else None,
            'boll20_mid': round(middle, 3), 'boll20_upper': round(upper, 3),
            'boll20_lower': round(lower, 3), 'state': state,
            'unsupported': ['minute_kdj', 'minute_atr', 'minute_adx'],
            'decision_boundary': 'observation_only_not_bs_or_order'}

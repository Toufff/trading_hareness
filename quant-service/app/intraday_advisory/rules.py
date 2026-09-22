"""Pure rolling quote rules for bounded intraday advisory signals.

The vendor's outer/inner volume is used only as an active-side proxy.  It is
never labelled as institutional or "main force" money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from statistics import median
from math import isfinite
from typing import Any, Sequence


@dataclass(frozen=True)
class QuoteSample:
    symbol: str
    observed_at: datetime
    price: float
    pre_close: float
    amount: float
    volume_lot: float
    outer_lot: float | None
    inner_lot: float | None
    name: str = ""
    bid_depth: float | None = None
    ask_depth: float | None = None


@dataclass(frozen=True)
class AdvisorySignal:
    event_key: str
    symbol: str
    name: str
    kind: str
    direction: str
    severity: str
    observed_at: datetime
    metrics: dict[str, Any]
    summary: str


def sample_from_row(row: dict[str, Any], observed_at: datetime) -> QuoteSample | None:
    def optional(value: Any) -> float | None:
        try:
            number = float(value)
            return number if isfinite(number) and number >= 0 else None
        except (TypeError, ValueError):
            return None

    def depth(key: str) -> float | None:
        levels = row.get(key)
        if not isinstance(levels, list) or len(levels) != 5:
            return None
        sizes = [optional(x.get('size')) if isinstance(x, dict) and
                 (optional(x.get('price')) or 0) > 0 else None for x in levels]
        return sum(sizes) if all(x is not None for x in sizes) else None

    try:
        sample = QuoteSample(
            symbol=str(row["ts_code"]), name=str(row.get("name") or ""), observed_at=observed_at,
            price=float(row["price"]), pre_close=float(row["pre_close"]),
            amount=float(row.get("cumulative_amount") or 0),
            volume_lot=float(row.get("cumulative_volume_lot") or 0),
            outer_lot=optional(row.get("outer_volume_lot")),
            inner_lot=optional(row.get("inner_volume_lot")),
            bid_depth=depth('bids'), ask_depth=depth('asks'),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(isfinite(x) for x in (sample.price, sample.pre_close, sample.amount, sample.volume_lot)) or sample.price <= 0 or sample.pre_close <= 0 or sample.amount < 0 or sample.volume_lot < 0:
        return None
    return sample


def _at_or_before(samples: Sequence[QuoteSample], target: datetime) -> QuoteSample | None:
    return next((item for item in reversed(samples) if item.observed_at <= target), None)


def _pct(new: float, old: float) -> float:
    return (new / old - 1.0) * 100 if old > 0 else 0.0


def _key(sample: QuoteSample, kind: str, direction: str) -> str:
    bucket = int(sample.observed_at.timestamp()) // 600
    return sha256(f"{sample.symbol}|{kind}|{direction}|{bucket}".encode()).hexdigest()


def _event(sample: QuoteSample, kind: str, direction: str, severity: str,
           metrics: dict[str, float], summary: str) -> AdvisorySignal:
    return AdvisorySignal(_key(sample, kind, direction), sample.symbol, sample.name, kind,
                          direction, severity, sample.observed_at, metrics, summary)


def evaluate(samples: Sequence[QuoteSample]) -> tuple[AdvisorySignal, ...]:
    """Evaluate a chronological series; at least 65 seconds of history is required."""
    if len(samples) < 2:
        return ()
    current = samples[-1]
    if current.observed_at - samples[0].observed_at < timedelta(seconds=65):
        return ()
    events: list[AdvisorySignal] = []
    for seconds, threshold in ((60, 1.2), (180, 2.0), (300, 3.0)):
        prior = _at_or_before(samples, current.observed_at - timedelta(seconds=seconds))
        if prior is None:
            continue
        change = _pct(current.price, prior.price)
        if abs(change) >= threshold:
            direction = "up" if change > 0 else "down"
            severity = "high" if abs(change) >= threshold * 1.5 else "medium"
            events.append(_event(
                current, f"price_{seconds}s", direction, severity,
                {"window_seconds": float(seconds), "price_change_pct": round(change, 3),
                 "price": current.price, "pre_close_change_pct": round(_pct(current.price, current.pre_close), 3)},
                f"{seconds // 60}分钟价格{'上涨' if change > 0 else '下跌'} {abs(change):.2f}%",
            ))

    prior_60 = _at_or_before(samples, current.observed_at - timedelta(seconds=60))
    if prior_60 is not None:
        amount_delta = max(0.0, current.amount - prior_60.amount)
        volume_delta = max(0.0, current.volume_lot - prior_60.volume_lot)
        if any(x is None for x in (current.outer_lot, prior_60.outer_lot, current.inner_lot, prior_60.inner_lot)):
            return tuple(events)
        active_net = (current.outer_lot - prior_60.outer_lot) - (current.inner_lot - prior_60.inner_lot)
        active_ratio = active_net / volume_delta if volume_delta > 0 else 0.0
        baselines: list[float] = []
        for minutes_back in range(2, 9):
            end = _at_or_before(samples, current.observed_at - timedelta(minutes=minutes_back - 1))
            start = _at_or_before(samples, current.observed_at - timedelta(minutes=minutes_back))
            if end and start and end.amount >= start.amount:
                baselines.append(end.amount - start.amount)
        normal = median([value for value in baselines if value > 0]) if any(value > 0 for value in baselines) else 0.0
        ratio = amount_delta / normal if normal > 0 else 0.0
        price_change = _pct(current.price, prior_60.price)
        if amount_delta >= 5_000_000 and ratio >= 3.0 and abs(active_ratio) >= 0.25:
            direction = "inflow" if active_ratio > 0 else "outflow"
            confirmed = price_change >= 0.15 if active_ratio > 0 else price_change <= -0.15
            if confirmed or abs(active_ratio) >= 0.45:
                events.append(_event(
                    current, "amount_pulse", direction,
                    "high" if ratio >= 5 or abs(active_ratio) >= 0.5 else "medium",
                    {"amount_delta": round(amount_delta, 2), "amount_ratio": round(ratio, 2),
                     "active_net_lot": round(active_net, 2), "active_ratio": round(active_ratio, 4),
                     "price_change_pct": round(price_change, 3)},
                    f"1分钟成交额放大至基线 {ratio:.1f} 倍，{'外盘增量占优' if active_ratio > 0 else '内盘增量占优'}",
                ))
    # Prefer the strongest signal of each kind/direction in one evaluation.
    unique: dict[tuple[str, str], AdvisorySignal] = {}
    for event in events:
        key = (event.kind, event.direction)
        if key not in unique or event.severity == "high":
            unique[key] = event
    return tuple(unique.values())


__all__ = ["AdvisorySignal", "QuoteSample", "evaluate", "sample_from_row"]

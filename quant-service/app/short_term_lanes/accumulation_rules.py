"""Pure scoring model for accumulation-like sideways close setups.

The module intentionally contains no I/O.  It turns one security's dated
vendor order-size flow, closing prices and current activity into continuous
scores.  The caller remains responsible for source provenance, coverage,
persistence and research/trade authorization.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable



def _number(value: Any) -> float | None:
    if value in (None, "", "-") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clip(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return min(upper, max(lower, value))


def _weighted_mean(items: Iterable[tuple[float, float]]) -> float:
    values = [(float(value), float(weight)) for value, weight in items if weight > 0]
    denominator = sum(weight for _, weight in values)
    if not denominator:
        return 0.0
    return sum(value * weight for value, weight in values) / denominator


def _harmonic_mean(left: float, right: float) -> float:
    if left <= 0 or right <= 0:
        return 0.0
    return 2.0 * left * right / (left + right)


def _daily_returns(closes: list[float]) -> list[float]:
    return [
        (closes[index] / closes[index - 1] - 1.0) * 100.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]


def _normalized_slope_pct(closes: list[float]) -> float:
    """OLS fitted movement over the full window, expressed as a percentage."""

    if len(closes) < 2:
        return 0.0
    x_mean = (len(closes) - 1) / 2.0
    y_mean = statistics.fmean(closes)
    denominator = sum((index - x_mean) ** 2 for index in range(len(closes)))
    if denominator <= 0 or y_mean <= 0:
        return 0.0
    slope = sum(
        (index - x_mean) * (value - y_mean)
        for index, value in enumerate(closes)
    ) / denominator
    return slope * (len(closes) - 1) / y_mean * 100.0


def _flow_window(rows: list[dict], window: int) -> dict | None:
    if len(rows) < window:
        return None
    selected = rows[-window:]
    values = [float(row["main_net"]) for row in selected]
    net = sum(values)
    gross = sum(abs(value) for value in values)
    positive_values = [max(0.0, value) for value in values]
    positive_total = sum(positive_values)
    positive_days = sum(value > 0 for value in values)
    amounts = [_number(row.get("amount")) for row in selected]
    has_amounts = all(value is not None and value > 0 for value in amounts)

    if has_amounts:
        amount_total = sum(float(value) for value in amounts if value is not None)
        intensity_pct = net / amount_total * 100.0 if amount_total else 0.0
        direction_score = 50.0 + 50.0 * math.tanh(intensity_pct / 1.25)
    else:
        normalized_net = net / gross if gross else 0.0
        intensity_pct = None
        direction_score = 50.0 + 50.0 * math.tanh(normalized_net / 0.40)

    recency_weights = list(range(1, window + 1))
    weighted_net = sum(
        value * weight for value, weight in zip(values, recency_weights)
    )
    if has_amounts:
        weighted_amount = sum(
            float(amount) * weight
            for amount, weight in zip(amounts, recency_weights)
            if amount is not None
        )
        recency_signal = weighted_net / weighted_amount * 100.0 if weighted_amount else 0.0
        recency_score = 50.0 + 50.0 * math.tanh(recency_signal / 1.25)
    else:
        weighted_gross = sum(
            abs(value) * weight for value, weight in zip(values, recency_weights)
        )
        recency_signal = weighted_net / weighted_gross if weighted_gross else 0.0
        recency_score = 50.0 + 50.0 * math.tanh(recency_signal / 0.40)

    density_score = positive_days / window * 100.0
    score = 0.50 * direction_score + 0.30 * density_score + 0.20 * recency_score
    dominance = max(positive_values) / positive_total if positive_total > 0 else 1.0
    return {
        "window": window,
        "net": net,
        "positive_days": positive_days,
        "positive_ratio": positive_days / window,
        "intensity_pct": intensity_pct,
        "single_day_positive_dominance": dominance,
        "direction_score": direction_score,
        "recency_score": recency_score,
        "density_score": density_score,
        "raw_score": score,
    }


def score_flow(rows: list[dict], windows: tuple[int, ...]) -> dict:
    configured_weights = {3: 0.25, 5: 0.50, 10: 0.25}
    details = [item for window in windows if (item := _flow_window(rows, window))]
    score = _weighted_mean(
        (item["raw_score"], configured_weights.get(item["window"], 1.0))
        for item in details
    )
    primary = next((item for item in details if item["window"] == 5), None)
    longest = max(details, key=lambda item: item["window"], default=None)
    dominance_source = primary or longest
    dominance = (
        float(dominance_source["single_day_positive_dominance"])
        if dominance_source
        else 1.0
    )
    dominance_penalty = _clip((dominance - 0.70) / 0.30 * 30.0, 0.0, 30.0)
    latest_negative_penalty = 4.0 if rows and float(rows[-1]["main_net"]) < 0 else 0.0
    score = _clip(score - dominance_penalty - latest_negative_penalty)
    return {
        "score": score,
        "windows": details,
        "primary_window": 5 if primary else (longest or {}).get("window"),
        "five_day_net": (primary or {}).get("net"),
        "five_day_positive_days": (primary or {}).get("positive_days"),
        "five_day_positive_ratio": (primary or {}).get("positive_ratio"),
        "five_day_intensity_pct": (primary or {}).get("intensity_pct"),
        "single_day_positive_dominance": dominance if dominance_source else None,
        "dominance_penalty": dominance_penalty,
        "latest_negative_penalty": latest_negative_penalty,
    }


def _sideways_window(
    closes: list[float],
    window: int,
    baseline_abs_move: float,
    baseline_volatility: float,
) -> dict | None:
    if len(closes) < window:
        return None
    selected = closes[-window:]
    returns = _daily_returns(selected)
    return_pct = (selected[-1] / selected[0] - 1.0) * 100.0
    range_pct = (max(selected) / min(selected) - 1.0) * 100.0
    fitted_move_pct = _normalized_slope_pct(selected)
    return_limit = _clip(
        1.25 + baseline_abs_move * math.sqrt(window), 1.50, 6.00
    )
    range_limit = _clip(
        2.50 + 1.20 * baseline_abs_move * math.sqrt(window), 3.00, 10.00
    )
    current_volatility = (
        statistics.pstdev(returns) if len(returns) >= 2 else baseline_volatility
    )

    return_score = 100.0 * math.exp(-1.50 * (abs(return_pct) / return_limit) ** 2)
    range_score = 100.0 * math.exp(-1.50 * (range_pct / range_limit) ** 2)
    slope_score = 100.0 * math.exp(
        -1.50 * (abs(fitted_move_pct) / return_limit) ** 2
    )
    shape_score = _harmonic_mean(
        _harmonic_mean(return_score, range_score), slope_score
    )
    volatility_ratio = current_volatility / max(baseline_volatility, 0.10)
    contraction_score = _clip(200.0 / (1.0 + max(0.0, volatility_ratio)))
    score = 0.85 * shape_score + 0.15 * contraction_score
    return {
        "window": window,
        "return_pct": return_pct,
        "close_range_pct": range_pct,
        "fitted_move_pct": fitted_move_pct,
        "adaptive_return_limit_pct": return_limit,
        "adaptive_range_limit_pct": range_limit,
        "realized_volatility_pct": current_volatility,
        "volatility_ratio": volatility_ratio,
        "raw_score": score,
    }


def score_sideways(rows: list[dict], windows: tuple[int, ...]) -> dict:
    closes = [
        float(value)
        for row in rows
        if (value := _number(row.get("close"))) is not None and value > 0
    ]
    all_returns = _daily_returns(closes)
    abs_returns = [abs(value) for value in all_returns]
    baseline_abs_move = statistics.median(abs_returns) if abs_returns else 1.0
    baseline_volatility = (
        statistics.pstdev(all_returns) if len(all_returns) >= 2 else baseline_abs_move
    )
    baseline_abs_move = max(0.10, baseline_abs_move)
    baseline_volatility = max(0.10, baseline_volatility)
    configured_weights = {5: 0.50, 8: 0.30, 10: 0.20, 13: 0.20}
    details = [
        item
        for window in windows
        if (
            item := _sideways_window(
                closes, window, baseline_abs_move, baseline_volatility
            )
        )
    ]
    score = _weighted_mean(
        (item["raw_score"], configured_weights.get(item["window"], 1.0))
        for item in details
    )
    five = next((item for item in details if item["window"] == 5), None)
    best = max(details, key=lambda item: item["raw_score"], default=None)
    max_abs_daily = max(abs_returns[-10:], default=0.0)
    path_limit = max(5.0, 3.0 * baseline_abs_move)
    path_penalty = _clip((max_abs_daily - path_limit) / max(path_limit, 1.0) * 20.0)
    score = _clip(score - path_penalty)
    return {
        "score": score,
        "windows": details,
        "best_window": (best or {}).get("window"),
        "five_day_return_pct": (five or {}).get("return_pct"),
        "five_day_range_pct": (five or {}).get("close_range_pct"),
        "baseline_abs_daily_move_pct": baseline_abs_move,
        "baseline_volatility_pct": baseline_volatility,
        "max_abs_daily_change_pct": max_abs_daily if all_returns else None,
        "path_penalty": path_penalty,
    }



MODEL_VERSION = "accumulation-multi-window-2026-09-10"

def evaluate(rows: list[dict]) -> dict:
    flow = score_flow(rows, (3, 5, 10))
    sideways = score_sideways(rows, (5, 8, 10))
    return {"version": MODEL_VERSION, "flow": flow, "sideways": sideways,
            "core_score": _harmonic_mean(flow["score"], sideways["score"]),
            "matched_flow": flow["score"] >= 55,
            "matched_sideways": sideways["score"] >= 55,
            "matched_intersection": flow["score"] >= 55 and sideways["score"] >= 55,
            "near_match": flow["score"] >= 45 and sideways["score"] >= 45}

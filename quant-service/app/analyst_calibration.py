"""Small, dependency-free, point-in-time probability calibration primitives.

These functions are deliberately conservative: they only emit out-of-fold
probabilities after an expanding chronological training window and return an
explicit insufficient-history state otherwise.  They are research artifacts,
never a live strategy input.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean
from typing import Any


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def _fit_platt(rows: list[tuple[float, int]]) -> tuple[float, float]:
    """Fit a bounded two-parameter logistic calibration with fixed steps."""
    intercept, slope = 0.0, 1.0
    for _ in range(80):
        grad_i = grad_s = 0.0
        for score, label in rows:
            error = _sigmoid(intercept + slope * score) - label
            grad_i += error
            grad_s += error * score
        scale = max(1, len(rows))
        intercept -= 0.15 * grad_i / scale
        slope -= 0.15 * grad_s / scale
        intercept = max(-8.0, min(8.0, intercept))
        slope = max(-8.0, min(8.0, slope))
    return intercept, slope


def _metrics(probabilities: list[float], labels: list[int]) -> dict[str, Any]:
    if not probabilities:
        return {"observations": 0, "brier": None, "log_loss": None, "hit_rate": None}
    clipped = [max(1e-6, min(1 - 1e-6, p)) for p in probabilities]
    return {
        "observations": len(labels),
        "brier": mean((p - y) ** 2 for p, y in zip(clipped, labels)),
        "log_loss": mean(-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in zip(clipped, labels)),
        "hit_rate": mean(float((p >= 0.5) == bool(y)) for p, y in zip(clipped, labels)),
    }


def reliability_bins(probabilities: list[float], labels: list[int], bins: int = 5) -> list[dict[str, Any]]:
    result = []
    for bucket in range(max(1, bins)):
        lower, upper = bucket / bins, (bucket + 1) / bins
        indices = [i for i, probability in enumerate(probabilities) if lower <= probability < upper or (bucket == bins - 1 and probability == upper)]
        if not indices:
            continue
        result.append({"lower": lower, "upper": upper, "observations": len(indices),
                       "mean_probability": mean(probabilities[i] for i in indices),
                       "empirical_rate": mean(labels[i] for i in indices)})
    return result


def chronological_calibration(
    events: list[dict[str, Any]], *, min_training_events: int = 30,
    embargo_days: int = 1, minimum_oof_events: int = 20,
) -> dict[str, Any]:
    """Evaluate Platt calibration against an expanding, embargoed timeline.

    Every event must contain ``event_date``, numeric ``score`` and binary
    ``label``. An optional ``exit_date`` marks when that event's own label
    actually became observable (e.g. the horizon's settlement date); when
    present, the embargo is enforced against ``exit_date`` rather than
    ``event_date``, because ``label`` is derived from a return realized up to
    ``exit_date``, and an event dated only a day or two after another one may
    still be settled long after it under a multi-day horizon: an ``d+2``
    event trained on a ``d`` event's label would otherwise use a label that
    was not yet known on ``d+2``. Events without ``exit_date`` fall back to
    treating it as ``event_date`` plus ``embargo_days`` (the previous,
    date-only behaviour), so the fallback error is conservative rather than
    permissive when the caller has not been updated to supply it.
    """
    normalized = []
    for event in events:
        try:
            event_date = event["event_date"] if isinstance(event["event_date"], date) else date.fromisoformat(str(event["event_date"]))
            label = int(event["label"])
            score = float(event["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if label not in (0, 1) or not math.isfinite(score):
            continue
        exit_date_raw = event.get("exit_date")
        try:
            exit_date = (exit_date_raw if isinstance(exit_date_raw, date) else date.fromisoformat(str(exit_date_raw))) \
                if exit_date_raw is not None else event_date + timedelta(days=max(0, embargo_days))
        except (TypeError, ValueError):
            exit_date = event_date + timedelta(days=max(0, embargo_days))
        normalized.append({"event_date": event_date, "label": label, "score": score, "exit_date": exit_date})
    normalized.sort(key=lambda row: row["event_date"])
    dates = sorted({row["event_date"] for row in normalized})
    predictions: list[dict[str, Any]] = []
    for row in normalized:
        train = [item for item in normalized if item["exit_date"] < row["event_date"]]
        if len(train) < min_training_events:
            continue
        intercept, slope = _fit_platt([(item["score"], item["label"]) for item in train])
        probability = _sigmoid(intercept + slope * row["score"])
        predictions.append({"event_date": str(row["event_date"]), "probability": probability, "label": row["label"], "training_events": len(train)})
    probabilities = [row["probability"] for row in predictions]
    labels = [row["label"] for row in predictions]
    baseline_probabilities = []
    for row in predictions:
        row_event_date = date.fromisoformat(row["event_date"])
        train = [item for item in normalized if item["exit_date"] < row_event_date]
        baseline_probabilities.append(mean(item["label"] for item in train) if train else 0.5)
    status = "completed" if len(predictions) >= minimum_oof_events else "insufficient_history"
    model_metrics = _metrics(probabilities, labels)
    baseline_metrics = _metrics(baseline_probabilities, labels)
    return {
        "status": status, "method": "expanding_platt_oof_v1", "events": len(normalized),
        "event_dates": len(dates), "oof_events": len(predictions),
        "minimum_training_events": min_training_events, "embargo_days": embargo_days,
        "minimum_oof_events": minimum_oof_events,
        "model": {**model_metrics, "reliability": reliability_bins(probabilities, labels)},
        "baseline": {**baseline_metrics, "reliability": reliability_bins(baseline_probabilities, labels)},
        "live_effect": "none", "predictions": predictions[-100:],
    }


__all__ = ["chronological_calibration", "reliability_bins"]

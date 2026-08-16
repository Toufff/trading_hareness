"""Offline probability diagnostics for delayed intraday outcomes.

No function in this module fits or updates a live strategy.  The calibration
contract is designed for out-of-fold, complete-trading-day predictions after
the replay foundation is available.  Until then the live service may only use
the interval helper to communicate uncertainty around its existing shrunk
historical base rate.
"""

from __future__ import annotations

import math
from typing import Any, Iterable


CALIBRATION_VERSION = "probability-calibration-v1"


def wilson_interval(successes: float, trials: float, *, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    """Return a stable binomial interval, including fractional prior mass."""
    n = float(trials or 0)
    if not math.isfinite(n) or n <= 0:
        return None, None
    rate = min(1.0, max(0.0, float(successes) / n))
    denominator = 1.0 + (z * z / n)
    centre = (rate + z * z / (2.0 * n)) / denominator
    spread = z * math.sqrt((rate * (1.0 - rate) + z * z / (4.0 * n)) / n) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def shrunk_probability_interval(
    *, raw_positive_rate: float | None, independent_days: int,
    prior_rate: float, prior_strength: float,
) -> dict[str, Any]:
    """Expose uncertainty without pretending correlated symbols are independent."""
    days = max(0, int(independent_days or 0))
    if raw_positive_rate is None or days <= 0:
        return {
            "lower": None, "upper": None, "method": "unavailable_no_independent_outcomes",
            "effective_trials": 0,
        }
    rate = min(1.0, max(0.0, float(raw_positive_rate)))
    strength = max(0.0, float(prior_strength))
    lower, upper = wilson_interval(rate * days + prior_rate * strength, days + strength)
    return {
        "lower": round(lower, 4) if lower is not None else None,
        "upper": round(upper, 4) if upper is not None else None,
        "method": "wilson_interval_on_beta_shrunk_trading_day_effective_sample",
        "effective_trials": round(days + strength, 4),
    }


def out_of_fold_calibration_diagnostics(rows: Iterable[dict[str, Any]], *, bins: int = 10) -> dict[str, Any]:
    """Measure only precomputed OOF predictions; no refit or online learning.

    Expected input rows: ``probability`` in [0,1], ``outcome`` in {0,1}, and
    a complete exchange date.  Callers must split by whole date before passing
    rows here.  This intentionally returns insufficient status for a small
    sample rather than a flattering pseudo-calibration curve.
    """
    clean: list[tuple[float, int, str]] = []
    for row in rows:
        try:
            probability, outcome = float(row.get("probability")), int(row.get("outcome"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(probability) and 0.0 <= probability <= 1.0 and outcome in {0, 1}):
            continue
        exchange_date = str(row.get("exchange_date") or "")
        if exchange_date:
            clean.append((probability, outcome, exchange_date))
    dates = {item[2] for item in clean}
    required_rows, required_days = 200, 60
    if len(clean) < required_rows or len(dates) < required_days:
        return {
            "status": "insufficient_oof_evidence", "version": CALIBRATION_VERSION,
            "rows": len(clean), "independent_trading_days": len(dates),
            "required_rows": required_rows, "required_trading_days": required_days,
            "notice": "No calibration fit or live-probability promotion is permitted below the gate.",
        }
    bin_count = max(2, min(20, int(bins)))
    groups: list[list[tuple[float, int, str]]] = [[] for _ in range(bin_count)]
    for item in clean:
        groups[min(bin_count - 1, int(item[0] * bin_count))].append(item)
    reliability = []
    for index, items in enumerate(groups):
        if not items:
            continue
        predicted = sum(item[0] for item in items) / len(items)
        realised = sum(item[1] for item in items) / len(items)
        reliability.append({
            "bin": index, "rows": len(items), "predicted": round(predicted, 6),
            "realised": round(realised, 6), "independent_trading_days": len({item[2] for item in items}),
        })
    brier = sum((item[0] - item[1]) ** 2 for item in clean) / len(clean)
    log_loss = -sum(
        item[1] * math.log(max(item[0], 1e-12)) + (1 - item[1]) * math.log(max(1 - item[0], 1e-12))
        for item in clean
    ) / len(clean)
    ece = sum(abs(item["predicted"] - item["realised"]) * item["rows"] for item in reliability) / len(clean)
    return {
        "status": "diagnostic_only", "version": CALIBRATION_VERSION,
        "rows": len(clean), "independent_trading_days": len(dates),
        "brier_score": round(brier, 6), "log_loss": round(log_loss, 6), "expected_calibration_error": round(ece, 6),
        "reliability": reliability,
        "policy": "OOF diagnostics only; formal promotion additionally requires replay, costs, stress tests and manual approval.",
    }


__all__ = [
    "CALIBRATION_VERSION", "out_of_fold_calibration_diagnostics",
    "shrunk_probability_interval", "wilson_interval",
]

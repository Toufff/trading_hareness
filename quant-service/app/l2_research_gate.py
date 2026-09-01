"""Fail-closed gate for deciding whether L2 is worth expanding.

L2 fields may be collected as research evidence, but this module never turns
them into a live threshold.  Expansion is allowed only after matched,
outcome-complete minute samples demonstrate statistically positive incremental
information over the existing Level-1/minute baseline.
"""

from __future__ import annotations

from math import sqrt
from statistics import mean, variance
from typing import Any, Iterable


def evaluate_l2_incremental_value(
    paired_rows: Iterable[dict[str, Any]],
    *,
    minimum_samples: int = 200,
    confidence_z: float = 1.96,
) -> dict[str, Any]:
    """Evaluate matched baseline/L2 outcome rows with a conservative CI."""
    deltas: list[float] = []
    source_versions: set[str] = set()
    for row in paired_rows:
        try:
            baseline = float(row["baseline_score"])
            level2 = float(row["l2_score"])
            outcome = float(row["outcome"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(map(lambda value: value == value and abs(value) != float("inf"), (baseline, level2, outcome))):
            continue
        # Compare absolute outcome residuals under the two frozen scores.  The
        # caller supplies the same matched label for each row.
        deltas.append(abs(outcome - baseline) - abs(outcome - level2))
        version = str(row.get("l2_algorithm_version") or "").strip()
        if version:
            source_versions.add(version)
    n = len(deltas)
    if n == 0:
        return {"status": "blocked", "reason": "no_matched_outcome_rows", "samples": 0, "live_effect": "none"}
    uplift = mean(deltas)
    standard_error = sqrt(variance(deltas) / n) if n > 1 else None
    lower = uplift - confidence_z * standard_error if standard_error is not None else None
    eligible = n >= max(1, int(minimum_samples)) and lower is not None and lower > 0
    return {
        "status": "eligible_for_research_expansion" if eligible else "blocked",
        "samples": n,
        "minimum_samples": int(minimum_samples),
        "mean_incremental_value": uplift,
        "ci95_lower": lower,
        "l2_algorithm_versions": sorted(source_versions),
        "live_effect": "none",
        "notice": "L2 remains research-only; no live threshold or order path is changed.",
    }


__all__ = ["evaluate_l2_incremental_value"]

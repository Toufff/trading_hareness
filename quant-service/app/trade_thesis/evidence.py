"""Point-in-time evidence eligibility and immutable manifest construction."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import isfinite
from typing import Any

from .contracts import ContractError, PointInTimeError


def amount_comparisons(current: float, previous: float, mean5: float) -> dict[str, float]:
    """Keep different volume baselines separate; this applies no trading threshold."""
    if previous <= 0 or mean5 <= 0:
        raise ContractError("amount_comparison_requires_positive_baselines")
    return {"versus_previous": current / previous, "versus_mean5": current / mean5}


def parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ContractError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"naive_{field}")
    return parsed


def filter_available_evidence(records: list[dict[str, Any]], cutoff_at: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter before feature construction; derived records require proven lineage."""
    cutoff = parse_time(cutoff_at, "cutoff_at")
    eligible, excluded = [], []
    ids = {r.get("evidence_id"): r for r in records if r.get("evidence_id")}
    for raw in records:
        record = deepcopy(raw)
        reason = None
        if not record.get("evidence_id") or not record.get("metric"):
            reason = "missing_identity_or_metric"
        elif isinstance(record.get("value"), float) and not isfinite(record["value"]):
            reason = "non_finite_value"
        elif not record.get("available_at"):
            reason = "availability_unknown"
        elif parse_time(record["available_at"], "available_at") > cutoff:
            reason = "available_after_cutoff"
        elif record.get("effective_at") and parse_time(record["effective_at"], "effective_at") > cutoff:
            reason = "effective_after_cutoff"
        elif record.get("superseded_at") and parse_time(record["superseded_at"], "superseded_at") <= cutoff:
            reason = "version_superseded"
        elif record.get("valid_until") and parse_time(record["valid_until"], "valid_until") < cutoff:
            reason = "evidence_stale"
        else:
            dependencies = record.get("dependencies")
            dependency_ids = record.get("dependency_ids") or []
            if record.get("derivation") and not (dependencies or dependency_ids):
                reason = "derived_lineage_unknown"
            for dependency in dependencies or []:
                available_at = dependency.get("available_at")
                if not dependency.get("evidence_id") or not available_at:
                    reason = "derived_lineage_unknown"
                    break
                if parse_time(available_at, "dependency_available_at") > cutoff:
                    reason = "dependency_available_after_cutoff"
                    break
            for dependency_id in dependency_ids:
                dependency = ids.get(dependency_id)
                if not dependency or not dependency.get("available_at"):
                    reason = "derived_lineage_unknown"
                    break
                if parse_time(dependency["available_at"], "dependency_available_at") > cutoff:
                    reason = "dependency_available_after_cutoff"
                    break
        if reason:
            excluded.append({"evidence_id": record.get("evidence_id"), "metric": record.get("metric"), "reason": reason})
        else:
            eligible.append(record)
    return eligible, excluded


def require_thesis_available(thesis: dict[str, Any], cutoff_at: str) -> None:
    cutoff = parse_time(cutoff_at, "cutoff_at")
    if parse_time(thesis["available_at"], "thesis_available_at") > cutoff:
        raise PointInTimeError("thesis_available_after_cutoff")
    if parse_time(thesis["effective_from"], "effective_from") > cutoff:
        raise PointInTimeError("thesis_effective_after_cutoff")

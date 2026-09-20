"""Quality reducers which never turn missing evidence into a decision."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def evidence_quality(eligible: list[dict[str, Any]], excluded: list[dict[str, Any]],
                     required_metrics: set[str], required_keys: set[tuple[str, str, str, str]] | None = None,
                     condition_results: list[dict[str, Any]] | None = None) -> str:
    values: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for item in eligible:
        key = (item["metric"], str(item.get("basis")), str(item.get("unit")), str(item.get("benchmark")))
        values[key].add(repr(item.get("value")))
    if any(len(distinct) > 1 for distinct in values.values()):
        return "conflict"
    if any(result.get("reason") == "evidence_conflict" for result in condition_results or []):
        return "conflict"
    if excluded and all(item.get("reason") == "evidence_stale" for item in excluded):
        return "stale"
    if not required_metrics or not required_keys:
        return "partial"
    present = {key[0] for key in values}
    if required_metrics - present or (required_keys or set()) - set(values) or excluded:
        return "partial"
    if any(result.get("result") == "unknown" for result in condition_results or []):
        return "partial"
    return "complete"


def required_metrics(conditions: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for condition in conditions:
        children = condition.get("all_of") or condition.get("any_of")
        if children:
            result.update(required_metrics(children))
        elif condition.get("metric"):
            result.add(condition["metric"])
    return result


def required_evidence_keys(conditions: list[dict[str, Any]]) -> set[tuple[str, str, str, str]]:
    result: set[tuple[str, str, str, str]] = set()
    for condition in conditions:
        children = condition.get("all_of") or condition.get("any_of")
        if children:
            result.update(required_evidence_keys(children))
        elif condition.get("metric"):
            result.add((condition["metric"], str(condition.get("basis")), str(condition.get("unit")),
                        str(condition.get("benchmark"))))
    return result

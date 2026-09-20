"""Pure tri-state thesis evaluator; no database, model or market I/O."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .contracts import immutable_thesis
from .evidence import filter_available_evidence, require_thesis_available
from .quality import evidence_quality, required_evidence_keys, required_metrics
from .transitions import enforce_no_renewal, thesis_state

SELECTORS = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "gt": lambda a, b: a > b,
             "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
             "in": lambda a, b: a in b, "between": lambda a, b: b[0] <= a <= b[1]}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     default=str, allow_nan=False).encode()).hexdigest()


def _combine(values: list[str], mode: str) -> str:
    if mode == "all_of":
        return "false" if "false" in values else ("unknown" if "unknown" in values else "true")
    return "true" if "true" in values else ("unknown" if "unknown" in values else "false")


def evaluate_condition(condition: dict[str, Any], by_metric: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    for mode in ("all_of", "any_of"):
        if mode in condition:
            children = [evaluate_condition(child, by_metric) for child in condition[mode]]
            return {"condition_id": condition["condition_id"], "purpose": condition["purpose"],
                    "result": _combine([child["result"] for child in children], mode), mode: children}
    supported_contract = {
        "window": "current",
        "confirmation": "single",
        "derivation": "identity",
        "evidence_requirement": "exact_basis_unit_benchmark",
    }
    unsupported = [field for field, expected in supported_contract.items() if condition.get(field) != expected]
    if unsupported:
        return {"condition_id": condition["condition_id"], "purpose": condition["purpose"],
                "metric": condition.get("metric"), "result": "unknown", "reason": "unsupported_contract",
                "unsupported_fields": unsupported}
    matches = [item for item in by_metric.get(condition["metric"], [])
               if item.get("basis") == condition["basis"] and item.get("unit") == condition["unit"] and
               item.get("benchmark") == condition["benchmark"]]
    if not matches:
        return {"condition_id": condition["condition_id"], "purpose": condition["purpose"],
                "metric": condition["metric"], "result": "unknown", "reason": "eligible_evidence_missing"}
    distinct = {repr(item.get("value")) for item in matches}
    if len(distinct) != 1:
        return {"condition_id": condition["condition_id"], "purpose": condition["purpose"],
                "metric": condition["metric"], "result": "unknown", "reason": "evidence_conflict",
                "evidence_ids": sorted(item["evidence_id"] for item in matches)}
    value = matches[-1].get("value")
    operator = condition["operator"]
    try:
        outcome = SELECTORS[operator](value, condition["threshold"])
    except (KeyError, TypeError, ValueError):
        outcome = None
    return {"condition_id": condition["condition_id"], "purpose": condition["purpose"], "metric": condition["metric"],
            "operator": operator, "threshold": condition["threshold"], "observed": value,
            "unit": condition["unit"], "basis": condition["basis"], "benchmark": condition["benchmark"],
            "result": "unknown" if outcome is None else str(bool(outcome)).lower(),
            "evidence_ids": sorted(item["evidence_id"] for item in matches)}


def evaluate_thesis(thesis: dict, evidence: list[dict], cutoff_at: str, previous: dict | None = None) -> dict:
    revision = immutable_thesis(thesis)
    require_thesis_available(revision, cutoff_at)
    enforce_no_renewal(revision, previous)
    eligible, excluded = filter_available_evidence(evidence, cutoff_at)
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for record in eligible:
        by_metric.setdefault(record["metric"], []).append(record)
    groups = {name: [evaluate_condition(c, by_metric) for c in revision.get(name, [])]
              for name in ("invariants", "confirmation_scenarios", "invalidation_conditions", "entry_conditions", "cancel_conditions")}
    flat = [result for values in groups.values() for result in values]
    truth = lambda name, value: any(result["result"] == value for result in groups[name])
    previous_state = ((previous or {}).get("states") or {}).get("thesis_state")
    state = thesis_state(cutoff_at=cutoff_at, deadline=revision["terminal_deadline"],
                         invalidated=truth("invalidation_conditions", "true"),
                         confirmed=bool(groups["confirmation_scenarios"]) and all(r["result"] == "true" for r in groups["confirmation_scenarios"]),
                         challenged=truth("invariants", "false"), previous_state=previous_state,
                         superseded=bool(revision.get("superseded_by")))
    deadline_passed = state == "expired"
    if deadline_passed:
        entry = "expired"
    elif truth("cancel_conditions", "true") or state in {"invalidated", "superseded"}:
        entry = "cancelled"
    elif not groups["entry_conditions"]:
        entry = "waiting"
    elif any(r["result"] == "unknown" for r in groups["entry_conditions"]):
        entry = "unknown"
    elif all(r["result"] == "true" for r in groups["entry_conditions"]):
        entry = "eligible"
    else:
        entry = "waiting"
    all_conditions = [c for name in groups for c in revision.get(name, [])]
    needed = required_metrics(all_conditions)
    status = evidence_quality(eligible, excluded, needed, required_evidence_keys(all_conditions), flat)
    if status != "complete" and entry == "eligible":
        entry = "suspended"
    def comparable_key(observation: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        return (observation.get("metric"), observation.get("basis"), observation.get("unit"), observation.get("benchmark"))
    prior_rows = (previous or {}).get("observations", [])
    prior_observations = {comparable_key(o): o for o in prior_rows}
    prior_by_metric: dict[str, list[dict[str, Any]]] = {}
    for old in prior_rows:
        prior_by_metric.setdefault(old.get("metric"), []).append(old)
    observations = [{"evidence_id": e["evidence_id"], "metric": e["metric"], "value": e.get("value"),
                     "unit": e.get("unit"), "basis": e.get("basis"), "benchmark": e.get("benchmark"),
                     "effective_at": e.get("effective_at"), "available_at": e["available_at"],
                     "availability_basis": e.get("availability_basis"), "version": e.get("version"),
                     "dependencies": e.get("dependencies") or [], "dependency_ids": e.get("dependency_ids") or []}
                    for e in eligible]
    changes = []
    for observation in observations:
        old = prior_observations.get(comparable_key(observation))
        impact, comparable = "observation", True
        if old is None and len(prior_by_metric.get(observation["metric"], [])) == 1:
            old = prior_by_metric[observation["metric"]][0]
            impact, comparable = "comparison_basis_changed", False
        if old is not None and (old.get("value") != observation.get("value") or not comparable):
            changes.append({"metric": observation["metric"], "old_value": old.get("value"),
                            "new_value": observation.get("value"), "unit": observation.get("unit"),
                            "old_basis": old.get("basis"), "basis": observation.get("basis"),
                            "old_benchmark": old.get("benchmark"), "benchmark": observation.get("benchmark"),
                            "evidence_id": observation["evidence_id"], "impact": impact,
                            "directly_comparable": comparable})
    contrary = [r for r in flat if (r["result"] == "true" and r.get("purpose") in {"invalidation", "soft_warning"}) or
                (r["result"] == "false" and r.get("purpose") == "structure_support")]
    next_checks = [{"condition_id": r["condition_id"], "metric": r.get("metric"), "reason": r.get("reason", "not_yet_satisfied")}
                   for r in flat if r["result"] in {"false", "unknown"}]
    core = {"thesis_id": revision["thesis_id"], "source_run_id": revision["source_run_id"], "cutoff_at": cutoff_at,
            "thesis_revision": revision["revision"], "terminal_deadline": revision["terminal_deadline"],
            "states": {"thesis_state": state, "evidence_status": status, "entry_state": entry},
            "observations": observations, "condition_results": flat, "changes_since_previous": changes,
            "contrary_evidence": contrary, "next_checks": next_checks, "reviewer_refs": list(revision.get("reviewer_refs", [])),
            "evidence_manifest": {"included": [e["evidence_id"] for e in eligible], "excluded": excluded,
                                  "pit_eligible": not excluded and status != "conflict"}}
    input_hash = _hash({"revision": revision, "eligible_evidence": eligible, "excluded": excluded,
                        "previous_evaluation_id": (previous or {}).get("evaluation_id"),
                        "previous_observations": (previous or {}).get("observations", []),
                        "evaluator_version": "trade_thesis_rules_v1"})
    core["input_hash"] = input_hash
    core["evaluation_id"] = _hash({"thesis_id": revision["thesis_id"], "revision": revision["revision"],
                                    "cutoff_at": cutoff_at, "input_hash": input_hash,
                                    "policy": revision.get("eligibility_policy_version")})[:32]
    core["content_hash"] = _hash(core)
    return core

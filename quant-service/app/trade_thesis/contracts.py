"""Typed JSON contracts for immutable thesis revisions and evaluations.

The persistence layer stores ordinary JSON.  These TypedDicts document that
wire shape while validation returns a deep copy so evaluation never mutates a
caller's revision or evidence records.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, NotRequired, TypedDict

TruthValue = Literal["true", "false", "unknown"]
ThesisState = Literal["pending", "supported", "challenged", "invalidated", "expired", "superseded"]
EvidenceStatus = Literal["complete", "partial", "stale", "conflict"]
EntryState = Literal["waiting", "eligible", "suspended", "cancelled", "expired", "unknown"]


class ContractError(ValueError):
    pass


class PointInTimeError(ContractError):
    pass


class AntiRenewalError(ContractError):
    pass


class Condition(TypedDict):
    condition_id: str
    purpose: str
    metric: str
    operator: str
    threshold: Any
    unit: str
    basis: str
    window: Any
    benchmark: Any
    confirmation: Any
    evidence_requirement: Any
    derivation: Any
    severity: str
    version: str
    all_of: NotRequired[list["Condition"]]
    any_of: NotRequired[list["Condition"]]


class Thesis(TypedDict):
    thesis_id: str
    revision: int
    symbol: str
    source_run_id: str
    claim: str
    available_at: str
    effective_from: str
    terminal_deadline: str
    origin_mode: Literal["prospective", "reconstructed", "unbound_legacy"]
    invariants: list[Condition]
    confirmation_scenarios: list[Condition]
    invalidation_conditions: list[Condition]
    entry_conditions: NotRequired[list[Condition]]
    cancel_conditions: NotRequired[list[Condition]]


REQUIRED_THESIS_FIELDS = (
    "thesis_id", "revision", "symbol", "source_run_id", "claim", "available_at",
    "effective_from", "terminal_deadline", "origin_mode", "invariants",
    "confirmation_scenarios", "invalidation_conditions",
)
REQUIRED_CONDITION_FIELDS = (
    "condition_id", "purpose", "metric", "operator", "threshold", "unit", "basis",
    "window", "benchmark", "confirmation", "evidence_requirement", "derivation",
    "severity", "version",
)


def immutable_thesis(value: dict[str, Any]) -> Thesis:
    missing = [field for field in REQUIRED_THESIS_FIELDS if field not in value]
    if missing:
        raise ContractError("missing_thesis_fields:" + ",".join(missing))
    if not isinstance(value["revision"], int) or value["revision"] < 1:
        raise ContractError("invalid_thesis_revision")
    if value["origin_mode"] not in {"prospective", "reconstructed", "unbound_legacy"}:
        raise ContractError("invalid_origin_mode")
    result = deepcopy(value)
    for group in ("invariants", "confirmation_scenarios", "invalidation_conditions", "entry_conditions", "cancel_conditions"):
        for condition in result.get(group, []):
            validate_condition(condition)
    if result.get('scenario') is not None:
        from .scenarios import Scenario
        if not isinstance(result['scenario'], dict):
            raise ContractError('scenario_must_be_mapping')
        Scenario.from_dict(result['scenario'])
    return result  # type: ignore[return-value]


def validate_condition(condition: dict[str, Any], _depth: int = 0) -> None:
    if _depth > 8:
        raise ContractError("condition_nesting_too_deep")
    composites = [key for key in ("all_of", "any_of") if key in condition]
    if composites:
        if len(composites) != 1 or not condition[composites[0]]:
            raise ContractError("invalid_condition_composite")
        for child in condition[composites[0]]:
            validate_condition(child, _depth + 1)
        # Composite nodes still carry identity/purpose/version, but have no fake metric.
        required = ("condition_id", "purpose", "version")
    else:
        required = REQUIRED_CONDITION_FIELDS
    missing = [field for field in required if field not in condition]
    if missing:
        raise ContractError("missing_condition_fields:" + ",".join(missing))

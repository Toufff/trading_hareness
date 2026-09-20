"""Advisory-only intersection of research scenarios and discipline facts.

This module does not evaluate account lines, size positions, mutate a plan or
infer a fill.  It consumes the already-evaluated thesis and discipline
contracts and keeps new-buy eligibility separate from an existing holding.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from .contracts import ContractError, validate_condition
from .evidence import parse_time
from .rules import evaluate_condition

ExecutionBasis = Literal["settled_daily_next_session", "forming_intraday"]

CONFLICT_MESSAGES = {
    "research_cancel_condition_triggered": "研究场景取消条件已触发",
    "scenario_thesis_revision_mismatch": "场景绑定的假设修订与当前评价不一致",
    "research_scenario_expired": "研究入场场景已过期",
    "binding_thesis_revision_mismatch": "账户绑定的假设修订与当前评价不一致",
    "binding_plan_mismatch": "账户绑定的纪律计划与当前计划不一致",
    "binding_symbol_mismatch": "账户绑定的股票与当前假设不一致",
    "binding_account_or_symbol_mismatch": "账户绑定与纪律计划的账户或股票不一致",
    "binding_after_evaluation_cutoff": "账户绑定发生在本次评价截止之后",
    "binding_time_unknown": "账户绑定时间不可验证",
    "discipline_plan_not_active": "纪律计划当前不是有效状态",
    "discipline_plan_quality_failed": "纪律计划质量检查未通过",
    "discipline_plan_expired": "纪律计划已过期",
    "discipline_risk_signal": "纪律计划已有风险动作信号，不能被研究看多覆盖",
    "discipline_evaluation_expired": "纪律评价已过期",
    "price_above_allowed_cap": "当前价格高于研究与纪律交集允许的最高入场价",
    "instrument_suspended": "标的停牌，不能推断可执行",
    "tradability_blocked": "可交易性证据显示当前不可执行",
    "tradability_unknown": "可交易性证据不足",
}


def _present_conflict(value: str) -> dict[str, str]:
    code, _, detail = value.partition(":")
    message = CONFLICT_MESSAGES.get(code, code.replace("_", " "))
    return {"code": code, "message": message, "detail": detail}


def _mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return deepcopy(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise ContractError("scenario_input_must_be_mapping_or_contract")


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    thesis_revision: int
    entry_conditions: tuple[dict[str, Any], ...]
    cancel_conditions: tuple[dict[str, Any], ...]
    max_entry_price: Decimal
    valid_until: str
    execution_basis: ExecutionBasis

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Scenario":
        required = ("scenario_id", "thesis_revision", "entry_conditions", "cancel_conditions",
                    "max_entry_price", "valid_until", "execution_basis")
        missing = [field for field in required if field not in value]
        if missing:
            raise ContractError("missing_scenario_fields:" + ",".join(missing))
        if not str(value["scenario_id"]).strip() or not isinstance(value["thesis_revision"], int) or value["thesis_revision"] < 1:
            raise ContractError("invalid_scenario_identity")
        price = _decimal(value["max_entry_price"])
        if price is None or price <= 0:
            raise ContractError("invalid_max_entry_price")
        parse_time(value["valid_until"], "scenario_valid_until")
        if value["execution_basis"] not in {"settled_daily_next_session", "forming_intraday"}:
            raise ContractError("invalid_execution_basis")
        entries, cancellations = deepcopy(value["entry_conditions"]), deepcopy(value["cancel_conditions"])
        for condition in [*entries, *cancellations]:
            validate_condition(condition)
        return cls(str(value["scenario_id"]), value["thesis_revision"], tuple(entries), tuple(cancellations),
                   price, value["valid_until"], value["execution_basis"])


def _condition_projection(scenario: Scenario, thesis_evaluation: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for observation in thesis_evaluation.get("observations", []):
        if observation.get("metric") and observation.get("evidence_id"):
            by_metric.setdefault(observation["metric"], []).append(observation)
    projected, conflicts = [], []
    for kind, conditions in (("entry", scenario.entry_conditions), ("cancel", scenario.cancel_conditions)):
        for condition in conditions:
            result = evaluate_condition(condition, by_metric)
            result["scenario_role"] = kind
            projected.append(result)
            if kind == "cancel" and result.get("result") == "true":
                conflicts.append("research_cancel_condition_triggered:" + condition["condition_id"])
    return projected, conflicts


def evaluate_scenario_intersection(
    scenario: Scenario | dict[str, Any], thesis_evaluation: dict[str, Any], *,
    discipline_plan: Any = None, discipline_evaluation: Any = None,
    binding: dict[str, Any] | None = None, execution_evidence: dict[str, Any] | None = None,
    as_of: str,
) -> dict[str, Any]:
    """Combine facts without altering either source contract or inferring shares."""
    scenario = scenario if isinstance(scenario, Scenario) else Scenario.from_dict(scenario)
    plan, discipline = _mapping(discipline_plan), _mapping(discipline_evaluation)
    binding, execution = deepcopy(binding), deepcopy(execution_evidence or {})
    now = parse_time(as_of, "scenario_as_of")
    condition_results, conflicts = _condition_projection(scenario, thesis_evaluation)
    thesis_revision = thesis_evaluation.get("thesis_revision")
    if thesis_revision != scenario.thesis_revision:
        conflicts.append("scenario_thesis_revision_mismatch")
    research_state = (thesis_evaluation.get("states") or {}).get("entry_state", "unknown")
    entry_results = [item.get("result") for item in condition_results if item.get("scenario_role") == "entry"]
    if not entry_results or "unknown" in entry_results:
        scenario_entry_state = "unknown"
    elif "false" in entry_results:
        scenario_entry_state = "waiting"
    elif all(result == "true" for result in entry_results):
        scenario_entry_state = "eligible"
    else:
        scenario_entry_state = "unknown"
    expired = now > parse_time(scenario.valid_until, "scenario_valid_until")
    if expired:
        conflicts.append("research_scenario_expired")

    binding_status = "unbound"
    if binding:
        binding_status = "bound"
        if binding.get("thesis_id") != thesis_evaluation.get("thesis_id") or binding.get("revision") != thesis_revision:
            binding_status = "mismatch"
            conflicts.append("binding_thesis_revision_mismatch")
        if plan and binding.get("plan_id") and binding.get("plan_id") != plan.get("plan_id"):
            binding_status = "mismatch"
            conflicts.append("binding_plan_mismatch")
        if binding.get("symbol") != thesis_evaluation.get("symbol"):
            binding_status = "mismatch"
            conflicts.append("binding_symbol_mismatch")
        if plan and (binding.get("account_key") != plan.get("account_key") or binding.get("symbol") != plan.get("symbol")):
            binding_status = "mismatch"
            conflicts.append("binding_account_or_symbol_mismatch")
        try:
            if parse_time(str(binding.get("bound_at")), "binding_bound_at") > now:
                binding_status = "mismatch"
                conflicts.append("binding_after_evaluation_cutoff")
        except ContractError:
            binding_status = "mismatch"
            conflicts.append("binding_time_unknown")

    plan_state = (discipline or {}).get("plan_state")
    context_missing = []
    if plan is None:
        context_missing.append("discipline_plan_missing")
    if discipline is None:
        context_missing.append("discipline_evaluation_missing")
    if plan and plan.get("status") != "active":
        conflicts.append("discipline_plan_not_active")
    if plan and any(check.get("passed") is False for check in plan.get("quality") or []):
        conflicts.append("discipline_plan_quality_failed")
    if plan and parse_time(str(plan["valid_until"]), "discipline_valid_until") < now:
        conflicts.append("discipline_plan_expired")
    if plan_state in {"exit_signalled", "reduce_signalled"}:
        conflicts.append("discipline_risk_signal:" + plan_state)
    if plan_state == "expired":
        conflicts.append("discipline_evaluation_expired")

    discipline_price_cap = None
    if plan:
        explicit = (plan.get("entry_constraints") or {}).get("max_entry_price")
        discipline_price_cap = _decimal(explicit)
        if discipline_price_cap is None:
            caps = [_decimal(line.get("price")) for line in plan.get("lines") or [] if line.get("kind") == "chase_cap"]
            discipline_price_cap = next((cap for cap in caps if cap is not None and cap > 0), None)
    allowed_cap = min(filter(None, (scenario.max_entry_price, discipline_price_cap)))
    current_price = _decimal(execution.get("current_price"))
    if current_price is not None and current_price > allowed_cap:
        conflicts.append(f"price_above_allowed_cap:{current_price}>{allowed_cap}")

    missing_execution = [field for field in ("quote_available_at", "quote_valid_until", "current_price", "is_suspended", "tradability_status")
                         if field not in execution]
    if current_price is None or current_price <= 0:
        missing_execution.append("valid_current_price")
    quote_time_valid = False
    if execution.get("quote_available_at") and execution.get("quote_valid_until"):
        try:
            quote_available = parse_time(str(execution["quote_available_at"]), "quote_available_at")
            quote_valid_until = parse_time(str(execution["quote_valid_until"]), "quote_valid_until")
            quote_time_valid = quote_available <= now <= quote_valid_until and quote_available <= quote_valid_until
        except ContractError:
            quote_time_valid = False
        if not quote_time_valid:
            missing_execution.append("fresh_quote_window")
    if not isinstance(execution.get("is_suspended"), bool):
        missing_execution.append("known_suspension_status")
    if execution.get("tradability_status") not in {"verified", "blocked"}:
        missing_execution.append("recognized_tradability_status")
    if execution.get("is_suspended") is True:
        conflicts.append("instrument_suspended")
    if execution.get("tradability_status") == "blocked":
        conflicts.append("tradability_" + str(execution["tradability_status"]))

    if scenario.execution_basis == "settled_daily_next_session":
        execution_state = "next_session_recheck"
        earliest_session = execution.get("next_session")
        combined = "expired" if expired else "blocked" if conflicts else "unknown" if context_missing else "waiting"
        execution_reason = ("存在约束冲突，需先处理：" + "；".join(conflicts) if conflicts else
                            "缺少纪律上下文：" + "、".join(context_missing) if context_missing else
                            "收盘确认只能在收盘后得知；下一交易时段重新核验价格、风险与可交易性")
    elif conflicts:
        execution_state, earliest_session, combined = "blocked", None, "blocked"
        execution_reason = "存在约束冲突，不能新增仓位：" + "；".join(conflicts)
    elif context_missing:
        execution_state, earliest_session, combined = "unknown", None, "unknown"
        execution_reason = "缺少纪律上下文：" + "、".join(context_missing)
    elif missing_execution:
        execution_state, earliest_session, combined = "unknown", None, "unknown"
        execution_reason = "执行证据不足：" + "、".join(dict.fromkeys(missing_execution))
    elif research_state == "eligible" and scenario_entry_state == "eligible":
        execution_state, earliest_session, combined = "executable_unverified_fill", None, "eligible"
        execution_reason = "研究与当前约束均通过；仍不推断已成交或可实现收益"
    else:
        execution_state, earliest_session, combined = "not_ready", None, research_state if research_state in {"waiting", "expired"} else "unknown"
        execution_reason = "研究或场景入场条件尚未满足"

    # Existing holding authority is the discipline evaluation alone.  A new-buy
    # cancellation must never be translated into an exit.
    if binding_status != "bound":
        holding_action = "no_inferred_action"
    elif plan_state in {"exit_signalled", "reduce_signalled"}:
        holding_action = plan_state
    elif plan_state == "active":
        holding_action = "hold_plan_active"
    else:
        holding_action = "no_inferred_action"
    plan_current = bool(plan and plan.get("status") == "active" and
                        parse_time(str(plan["valid_until"]), "discipline_valid_until") >= now)
    position_quantity = (((plan or {}).get("position") or {}).get("quantity")
                         if binding_status == "bound" and plan_current else None)
    return {
        "scenario_id": scenario.scenario_id, "thesis_revision": scenario.thesis_revision,
        "research_entry_state": research_state, "scenario_entry_state": scenario_entry_state,
        "combined_entry_state": combined,
        "condition_results": condition_results, "conflict_codes": conflicts,
        "conflicts": [_present_conflict(item) for item in conflicts],
        "price_limits": {"research_max_entry_price": str(scenario.max_entry_price),
                         "discipline_max_entry_price": None if discipline_price_cap is None else str(discipline_price_cap),
                         "effective_max_entry_price": str(allowed_cap)},
        "execution": {"basis": scenario.execution_basis, "state": execution_state,
                      "earliest_session": earliest_session, "reason": execution_reason,
                      "verified_fill": False, "realized_return": None},
        "holding": {"binding_status": binding_status, "plan_state": plan_state,
                    "hard_risk": plan_state in {"exit_signalled", "reduce_signalled"},
                    "action": holding_action, "position_quantity": position_quantity,
                    "action_quantity": None},
        "advisory_only": True, "live_effect": "none", "decision_binding": False,
        "missing_context": context_missing,
    }


__all__ = ["Scenario", "evaluate_scenario_intersection"]

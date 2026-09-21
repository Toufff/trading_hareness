"""Versioned contracts for machine-derived trade discipline.

Research-only.  Nothing here connects to a broker, submits an order or changes
``personal_trade_plans``.  A plan is an append-only statement of what the system
derived from evidence at one point in time: every price carries the derivation
that produced it, and every line declares how it can be evaluated later.
"""

from __future__ import annotations

import ast
import math
import operator
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CONTRACT_VERSION = "trade-discipline-v1"

_BINARY_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                     ast.Div: operator.truediv, ast.Pow: operator.pow}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"min": min, "max": max, "abs": abs, "round": round, "floor": math.floor}


class FormulaError(ValueError):
    """Raised when a derivation formula is not a plain arithmetic expression."""


def eval_expression(expression: str, inputs: dict[str, Any]) -> float:
    """Evaluate an arithmetic derivation formula over its recorded inputs.

    Only numbers, the recorded input names, ``+ - * / **`` and
    ``min/max/abs/round/floor`` are accepted.  This is what makes
    ``Derivation`` an auditable object instead of a free-text note.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise FormulaError(f"unparsable formula: {expression}") from error

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError("only numeric constants are allowed")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in inputs:
                raise FormulaError(f"formula references unknown input {node.id!r}")
            value = inputs[node.id]
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise FormulaError(f"input {node.id!r} is not numeric")
            return float(value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            return float(_BINARY_OPERATORS[type(node.op)](walk(node.left), walk(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return float(_UNARY_OPERATORS[type(node.op)](walk(node.operand)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
            if node.keywords:
                raise FormulaError("keyword arguments are not allowed in a formula")
            return float(_FUNCTIONS[node.func.id](*(walk(argument) for argument in node.args)))
        raise FormulaError(f"unsupported expression node {type(node).__name__}")

    return walk(tree)

LineKind = Literal[
    "exposure", "hard_stop", "soft_stop", "trail", "time_stop", "no_add",
    "take_partial", "holiday", "trigger", "cancel", "chase_cap",
]
Metric = Literal["daily_close", "minute_close", "last", "low", "high", "vwap"]
Op = Literal["<", "<=", ">", ">="]
ExtraCondition = Literal[
    "sector_change_negative",   # owning first-level industry is down today (needs a stored membership)
    "sector_not_weak",          # owning first-level industry is flat or up today
    "amount_ge_prev_day",       # today's turnover >= yesterday's
    "volume_expand_1_5x",       # today's volume >= 1.5x the prior five-day mean
    "volume_contract_0_7x",     # today's volume <= 0.7x the prior five-day mean
    "below_vwap",               # last price below today's VWAP
    "after_volume_climax",      # today's volume >= the 20-day maximum and the close sits in the lower half
]
SECTOR_CONDITIONS = frozenset({"sector_change_negative", "sector_not_weak"})
ActionType = Literal[
    "exit_all", "reduce_to_shares", "reduce_by_pct", "move_stop_to",
    "block_add", "alert", "buy_up_to_shares",
]
Stage = Literal[
    "crash_rebound", "broken", "breakout_hold", "trend_hold",
    "pullback_hold", "base_platform", "unclassified",
]
PlanKind = Literal["holding", "new_buy"]
PlanStatus = Literal["active", "rejected_by_quality", "superseded", "expired"]
ExecuteBy = Literal["price", "time"]


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


class Confirm(BaseModel):
    bars: int = Field(default=1, ge=1, le=20)
    basis: Literal["daily", "minute"] = "daily"


class Derivation(BaseModel):
    """Every derived price must be recomputable from ``inputs`` via ``formula``.

    A line whose ``action.value`` is itself a derived number (the trail's
    ``move_stop_to`` target) carries a second pair, ``action_formula`` over
    ``action_inputs``, so the value the human is told to act on is as auditable
    as the price that triggers it.
    """

    rule_id: str = Field(min_length=1, max_length=120)
    inputs: dict[str, Any] = Field(default_factory=dict)
    formula: str = Field(default="", max_length=600)
    action_inputs: dict[str, Any] = Field(default_factory=dict)
    action_formula: str = Field(default="", max_length=600)

    def recompute(self) -> float:
        return eval_expression(self.formula, self.inputs)

    def recompute_action(self) -> float:
        return eval_expression(self.action_formula, self.action_inputs)


class Action(BaseModel):
    type: ActionType
    value: Decimal | int | None = None


class Line(BaseModel):
    kind: LineKind
    label: str = Field(min_length=1, max_length=200)
    metric: Metric | None = None
    op: Op | None = None
    price: Decimal | None = None
    pct: Decimal | None = None
    confirm: Confirm = Field(default_factory=Confirm)
    extra: list[ExtraCondition] = Field(default_factory=list)
    execute_by: ExecuteBy = "price"
    execute_at: str | None = Field(default=None, max_length=60)
    trading_days: int | None = Field(default=None, ge=1, le=40)
    action: Action
    derivation: Derivation
    priority: int = Field(ge=0, le=99)

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("price must be positive")
        return value


class Sizing(BaseModel):
    equity: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=10)
    reference_price: Decimal = Field(gt=0)
    hard_stop: Decimal = Field(gt=0)
    stop_distance: Decimal = Field(gt=0)
    risk_amount: Decimal = Field(gt=0)
    max_shares: int = Field(ge=0)
    target_exposure_pct: Decimal = Field(ge=0, le=100)
    current_shares: int = Field(ge=0)
    current_exposure_pct: Decimal = Field(ge=0)
    recommended_shares: int = Field(ge=0)
    # ``current_shares x stop_distance / equity``: the risk the position already
    # carries, printed next to the 1% budget.  Disclosure only, never a gate.
    current_risk_pct: Decimal | None = Field(default=None, ge=0)
    # Calibrated cap (``target_exposure_pct``) expressed in shares, which of the two limits binds
    # (``risk``: 1% / stop distance, ``cap``: extreme-loss cap) and the calibration cell behind the cap.
    cap_shares: int | None = Field(default=None, ge=0)
    binding_constraint: Literal["risk", "cap"] | None = None
    exposure_basis: dict[str, Any] | None = None
    # Where ``risk_per_trade_pct`` came from: the standing policy the user set,
    # or an operator's ``--risk-per-trade-pct``. Optional so plans written
    # before 2026-09-20 still load; before it existed, a default nobody had
    # chosen was indistinguishable from a decision the user had made.
    risk_policy: dict[str, Any] | None = None
    # The highest price this plan permits a fill at, and its distance to the
    # hard stop. Both share counts are computed here, not at reference_price,
    # so the tolerance holds everywhere inside the buy zone the card itself
    # authorises. Equal to reference_price on a holding. Optional so plans
    # written before 2026-09-20 still load.
    sizing_price: Decimal | None = Field(default=None, gt=0)
    sizing_distance: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_stop(self) -> "Sizing":
        if self.hard_stop >= self.reference_price:
            raise ValueError("hard_stop must sit below reference_price")
        return self


class PositionRef(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    quantity: int = Field(ge=0)
    sellable_quantity: int = Field(ge=0)
    # Keep the broker fact intact.  A negative diluted cost is displayable but
    # is not suitable as a stop/trigger price anchor; templates handle that
    # distinction explicitly.
    average_cost: Decimal | None = None
    market_price: Decimal | None = Field(default=None, ge=0)
    market_value: Decimal | None = Field(default=None, ge=0)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")

    @model_validator(mode="after")
    def validate_quantities(self) -> "PositionRef":
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable_quantity must not exceed quantity")
        return self


class QualityCheck(BaseModel):
    check_id: str = Field(min_length=1, max_length=80)
    passed: bool
    detail: str = Field(default="", max_length=400)


class DisciplinePlan(BaseModel):
    contract_version: str = CONTRACT_VERSION
    plan_key: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=64)
    account_key: str = Field(min_length=1, max_length=64)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=120)
    plan_kind: PlanKind
    stage: Stage
    template_key: str = Field(min_length=1, max_length=120)
    template_version: str = Field(min_length=1, max_length=60)
    as_of_at: datetime
    trading_date: date
    valid_until: datetime
    position: PositionRef | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    sizing: Sizing | None = None
    lines: list[Line] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    quality: list[QualityCheck] = Field(default_factory=list)
    status: PlanStatus = "active"
    supersedes_plan_id: str | None = None
    lowered_reason: str | None = Field(default=None, max_length=400)
    inputs_hash: str = Field(min_length=8, max_length=64)
    generator_version: str = Field(min_length=1, max_length=60)

    @field_validator("as_of_at", "valid_until")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return _aware(value, "timestamp")

    def lines_of(self, kind: LineKind) -> list[Line]:
        return [line for line in self.lines if line.kind == kind]

    @property
    def quality_passed(self) -> bool:
        return all(check.passed for check in self.quality)


class LineState(BaseModel):
    kind: LineKind
    label: str
    # ``capped``: a new-buy trigger whose confirming close sat above the
    # chase cap (entry_price + 0.5 x ATR14) - "已越过追高上限，不买".
    state: Literal["armed", "triggered", "expired", "cancelled", "capped"]
    triggered_at: datetime | None = None
    trigger_price: Decimal | None = None
    basis: str = "daily"
    evidence: dict[str, Any] = Field(default_factory=dict)


class Evaluation(BaseModel):
    plan_id: str
    as_of_at: datetime
    trading_date: date
    basis: Literal["daily", "minute"] = "daily"
    line_states: list[LineState] = Field(default_factory=list)
    plan_state: Literal["active", "exit_signalled", "reduce_signalled", "expired"] = "active"
    inputs_hash: str


class ComplianceRecord(BaseModel):
    plan_id: str
    trade_record_id: str | None = None
    line_kind: LineKind | None = None
    verdict: Literal["followed", "early", "late", "missed", "against_plan", "unplanned"]
    deviation: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class Review(BaseModel):
    plan_id: str
    reviewer: str = Field(min_length=1, max_length=80)
    verdict: Literal["accept", "override", "reject"]
    notes: str = ""
    overrides: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "Action", "ActionType", "CONTRACT_VERSION", "ComplianceRecord", "Confirm", "Derivation",
    "DisciplinePlan", "Evaluation", "ExecuteBy", "ExtraCondition", "FormulaError", "Line", "LineKind",
    "LineState", "Metric", "Op", "PlanKind", "PlanStatus", "PositionRef", "QualityCheck", "Review",
    "SECTOR_CONDITIONS", "Sizing", "Stage", "eval_expression",
]

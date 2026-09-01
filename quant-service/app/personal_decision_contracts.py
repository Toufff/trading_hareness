"""Versioned contracts for actual holdings and human-executed trade plans.

This domain is deliberately separate from paper trading.  It never connects to
a broker and never submits an order.  A caller may persist a verified read-only
snapshot or a terminal research plan; unfinished research belongs in the
research funnel, not in a human-facing decision brief.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


CONTRACT_VERSION = "personal-decision-v1"
BrokerVerification = Literal["verified_exact", "verified_partial"]
PlanKind = Literal["holding", "new_buy"]
PlanAction = Literal["hold", "observe", "buy_on_trigger", "reduce_on_trigger", "exit_on_trigger", "avoid"]


def _timezone_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


class PriceZone(BaseModel):
    lower: Decimal = Field(gt=0)
    upper: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> "PriceZone":
        if self.upper < self.lower:
            raise ValueError("upper must not be below lower")
        return self


class BrokerPositionInput(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=120)
    quantity: Decimal = Field(ge=0)
    sellable_quantity: Decimal = Field(ge=0)
    average_cost: Decimal | None = Field(default=None, ge=0)
    market_price: Decimal | None = Field(default=None, ge=0)
    market_value: Decimal | None = Field(default=None, ge=0)
    unrealized_pnl: Decimal | None = None
    position_weight_pct: Decimal | None = Field(default=None, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=40)

    @model_validator(mode="after")
    def validate_quantities(self) -> "BrokerPositionInput":
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable_quantity must not exceed quantity")
        return self


class BrokerPortfolioSnapshotInput(BaseModel):
    contract_version: Literal["personal-decision-v1"] = CONTRACT_VERSION
    account_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    source: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    source_snapshot_key: str = Field(min_length=1, max_length=160)
    observed_at: datetime
    verification: BrokerVerification
    cash: Decimal | None = Field(default=None, ge=0)
    total_asset: Decimal | None = Field(default=None, ge=0)
    total_market_value: Decimal | None = Field(default=None, ge=0)
    positions: list[BrokerPositionInput] = Field(default_factory=list, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=80)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _timezone_aware(value, "observed_at")

    @model_validator(mode="after")
    def validate_positions(self) -> "BrokerPortfolioSnapshotInput":
        symbols = [position.symbol for position in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("positions must contain each symbol at most once")
        if self.verification == "verified_exact" and self.total_market_value is not None:
            known_values = [position.market_value for position in self.positions]
            if known_values and all(value is not None for value in known_values):
                position_total = sum((value for value in known_values if value is not None), Decimal("0"))
                # Broker account totals and per-position rows are commonly rounded
                # at different display precisions.  A one-per-mille tolerance keeps
                # that harmless UI rounding admissible while still rejecting a
                # missing or duplicated position.
                tolerance = max(Decimal("1.00"), self.total_market_value * Decimal("0.001"))
                if abs(position_total - self.total_market_value) > tolerance:
                    raise ValueError("verified_exact position market values must reconcile to total_market_value")
        return self


class PersonalTradePlanInput(BaseModel):
    contract_version: Literal["personal-decision-v1"] = CONTRACT_VERSION
    plan_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    plan_kind: PlanKind
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=120)
    as_of_at: datetime
    valid_until: datetime
    action: PlanAction
    entry_zone: PriceZone | None = None
    add_trigger: str | None = Field(default=None, min_length=3, max_length=600)
    reduce_trigger: str | None = Field(default=None, min_length=3, max_length=600)
    exit_trigger: str = Field(min_length=3, max_length=600)
    stop_price: Decimal | None = Field(default=None, gt=0)
    target_prices: list[Decimal] = Field(default_factory=list, max_length=6)
    max_position_pct: Decimal = Field(ge=0, le=100)
    rationale: list[str] = Field(min_length=1, max_length=12)
    evidence_refs: list[str] = Field(min_length=1, max_length=40)
    risk_flags: list[str] = Field(default_factory=list, max_length=30)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=60)

    @field_validator("as_of_at", "valid_until")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return _timezone_aware(value, "plan timestamp")

    @field_validator("target_prices")
    @classmethod
    def validate_targets(cls, values: list[Decimal]) -> list[Decimal]:
        if any(value <= 0 for value in values):
            raise ValueError("target_prices must be positive")
        return values

    @model_validator(mode="after")
    def validate_executable_plan(self) -> "PersonalTradePlanInput":
        if self.valid_until <= self.as_of_at:
            raise ValueError("valid_until must be after as_of_at")
        if self.plan_kind == "new_buy" and self.action == "buy_on_trigger":
            missing = []
            if self.entry_zone is None:
                missing.append("entry_zone")
            if self.stop_price is None:
                missing.append("stop_price")
            if self.max_position_pct <= 0:
                missing.append("max_position_pct")
            if not self.target_prices:
                missing.append("target_prices")
            if missing:
                raise ValueError(f"new-buy plan is incomplete: {', '.join(missing)}")
        if self.plan_kind == "new_buy" and self.action in {"reduce_on_trigger", "exit_on_trigger"}:
            raise ValueError("new-buy plans cannot reduce or exit an existing position")
        return self


def assemble_personal_decision_brief(
    *,
    as_of_at: datetime,
    market_section: dict[str, Any] | None,
    portfolio: dict[str, Any] | None,
    plans: list[dict[str, Any]],
    max_portfolio_age: timedelta = timedelta(days=4),
    future_clock_tolerance: timedelta = timedelta(minutes=5),
) -> dict[str, Any]:
    """Assemble independent market, holding and new-buy sections.

    This projection never converts an unfinished research candidate into a
    visible plan.  Missing holding plans are diagnostic blockers rather than
    vague "research pending" prose in the user-facing action list.
    """
    _timezone_aware(as_of_at, "as_of_at")
    market_ready = bool(market_section and market_section.get("status") in {"ready", "completed"})
    observed_at = portfolio.get("observed_at") if portfolio else None
    if isinstance(observed_at, str):
        observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    portfolio_age = (
        as_of_at.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
        if isinstance(observed_at, datetime) and observed_at.tzinfo is not None
        else None
    )
    portfolio_current = bool(
        portfolio
        and portfolio.get("verification") == "verified_exact"
        and portfolio_age is not None
        and -future_clock_tolerance <= portfolio_age <= max_portfolio_age
    )
    usable_plans = []
    for raw in plans:
        try:
            plan = PersonalTradePlanInput.model_validate(raw)
        except ValueError:
            continue
        if plan.valid_until >= as_of_at:
            usable_plans.append(plan)
    latest_by_key: dict[tuple[str, str], PersonalTradePlanInput] = {}
    for plan in sorted(usable_plans, key=lambda item: item.as_of_at):
        latest_by_key[(plan.plan_kind, plan.symbol)] = plan

    positions = list(portfolio.get("positions") or []) if portfolio_current else []
    holding_actions = []
    missing_holding_plans = []
    for position in positions:
        symbol = str(position.get("symbol") or "")
        plan = latest_by_key.get(("holding", symbol))
        if plan is None:
            missing_holding_plans.append(symbol)
            continue
        holding_actions.append({"position": position, "plan": plan.model_dump(mode="json")})
    new_buy_actions = [
        plan.model_dump(mode="json")
        for (kind, _), plan in latest_by_key.items()
        if kind == "new_buy" and plan.action == "buy_on_trigger"
    ]
    holdings_ready = portfolio_current and not missing_holding_plans
    diagnostics = []
    if not market_ready:
        diagnostics.append("market_section_unavailable")
    if portfolio and not portfolio_current:
        diagnostics.append("portfolio_snapshot_stale_or_not_exact")
    elif not portfolio:
        diagnostics.append("portfolio_snapshot_missing")
    diagnostics.extend(f"holding_plan_missing:{symbol}" for symbol in missing_holding_plans)
    return {
        "contract_version": CONTRACT_VERSION,
        "as_of_at": as_of_at.isoformat(),
        "status": "ready" if market_ready and holdings_ready else "partial",
        "market": {"status": "ready" if market_ready else "unavailable", "content": market_section if market_ready else None},
        "holdings": {
            "status": "ready" if holdings_ready else "blocked",
            "portfolio_observed_at": observed_at.isoformat() if isinstance(observed_at, datetime) else None,
            "actions": holding_actions,
        },
        "new_buys": {"status": "ready", "actions": new_buy_actions},
        "delivery": {
            "market_eligible": market_ready,
            "holding_actions_eligible": holdings_ready,
            "new_buy_actions_eligible": bool(new_buy_actions),
        },
        "diagnostics": diagnostics,
        "boundary": "human_decision_support_only; no_broker_order_path",
    }


__all__ = [
    "CONTRACT_VERSION", "BrokerPortfolioSnapshotInput", "BrokerPositionInput",
    "PersonalTradePlanInput", "PriceZone", "assemble_personal_decision_brief",
]

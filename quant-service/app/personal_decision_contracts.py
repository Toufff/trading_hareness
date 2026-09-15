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
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator
from .broker_snapshot_freshness import broker_freshness
from .broker_desktop_evidence import SOURCES as DESKTOP_SOURCES, validate_desktop_metadata
from .broker_fact_sync_rules import validate_exact_totals


CONTRACT_VERSION = "personal-decision-v1"
BrokerVerification = Literal["verified_exact", "verified_partial"]
PlanKind = Literal["holding", "new_buy"]
PlanAction = Literal["hold", "observe", "buy_on_trigger", "reduce_on_trigger", "exit_on_trigger", "avoid"]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _timezone_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _market_trade_date(market_section: dict[str, Any] | None) -> date | None:
    if not market_section:
        return None
    return _date_value(market_section.get("exchange_date") or market_section.get("trading_date"))


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
        if self.source in DESKTOP_SOURCES:
            if self.verification != "verified_exact":
                raise ValueError("BROKER_DESKTOP_EXACT_REQUIRED")
            validate_desktop_metadata(self.metadata, self.source, self.observed_at, self.positions)
            validate_exact_totals(
                {"total_assets": self.total_asset, "market_value": self.total_market_value, "available_cash": self.cash},
                [{"quantity": row.quantity, "available_quantity": row.sellable_quantity,
                  "price": row.market_price, "market_value": row.market_value} for row in self.positions],
            )
        if self.verification == "verified_exact":
            if any(value is None for value in (self.total_market_value, self.total_asset, self.cash)):
                raise ValueError("verified_exact requires account total_asset, total_market_value and cash to reconcile")
            known_values = [position.market_value for position in self.positions]
            if any(value is None for value in known_values):
                raise ValueError("verified_exact requires every position market_value to reconcile")
            if all(value is not None for value in known_values):
                position_total = sum((value for value in known_values if value is not None), Decimal("0"))
                # Broker account totals and per-position rows are commonly rounded
                # at different display precisions.  A one-per-mille tolerance keeps
                # that harmless UI rounding admissible while still rejecting a
                # missing or duplicated position.
                tolerance = max(Decimal("1.00"), self.total_market_value * Decimal("0.001"))
                if abs(position_total - self.total_market_value) > tolerance:
                    raise ValueError("verified_exact position market values must reconcile to total_market_value")
            if self.source == 'citics_mumu_luna' and not (
                self.metadata.get('fresh_navigation_verified') is True
                and self.metadata.get('complete_positions_verified') is True
                and self.metadata.get('evidence')
                and self.metadata.get('controller_model') == 'gpt-5.6-luna'
            ):
                raise ValueError('verified_exact CITIC observation requires fresh Luna screenshot evidence')
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
    market_status = market_section.get("status") if market_section else None
    market_available = market_status in {"ready", "completed", "degraded"}
    market_complete = market_status in {"ready", "completed"}
    reference_trade_date = _market_trade_date(market_section)
    freshness = broker_freshness(portfolio, as_of_at, reference_trade_date,
                                 max_portfolio_age, future_clock_tolerance)
    observed_at, portfolio_age = freshness["observed_at"], freshness["age"]
    portfolio_current, portfolio_trade_date = freshness["current"], freshness["trade_date"]
    usable_plans = []
    stale_plans: list[tuple[str, str, str]] = []
    for raw in plans:
        try:
            plan = PersonalTradePlanInput.model_validate(raw)
        except ValueError:
            continue
        if plan.valid_until < as_of_at:
            continue
        plan_trade_date = _date_value(plan.as_of_at)
        if reference_trade_date and plan_trade_date and plan_trade_date < reference_trade_date:
            stale_plans.append((plan.plan_kind, plan.symbol, "older_than_latest_market"))
            continue
        if plan.plan_kind == "holding" and portfolio_current and portfolio:
            expected_snapshot_id = portfolio.get("snapshot_id")
            bound_snapshot_id = plan.metadata.get("portfolio_snapshot_id")
            if expected_snapshot_id is not None and str(bound_snapshot_id or "") != str(expected_snapshot_id):
                stale_plans.append((plan.plan_kind, plan.symbol, "not_bound_to_latest_portfolio"))
                continue
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
    if not market_available:
        diagnostics.append("market_section_unavailable")
    elif not market_complete:
        diagnostics.append("market_section_degraded")
    if portfolio and not portfolio_current:
        diagnostics.append("portfolio_snapshot_stale_or_not_exact")
        if reference_trade_date and portfolio_trade_date and portfolio_trade_date < reference_trade_date:
            diagnostics.append(
                f"portfolio_snapshot_older_than_market:{portfolio_trade_date}:{reference_trade_date}"
            )
    elif not portfolio:
        diagnostics.append("portfolio_snapshot_missing")
    diagnostics.extend(f"holding_plan_missing:{symbol}" for symbol in missing_holding_plans)
    diagnostics.extend(f"trade_plan_stale:{kind}:{symbol}:{reason}" for kind, symbol, reason in stale_plans)
    return {
        "contract_version": CONTRACT_VERSION,
        "as_of_at": as_of_at.isoformat(),
        "status": "ready" if market_complete and holdings_ready else "partial",
        "market": {"status": market_status if market_available else "unavailable",
                   "content": market_section if market_available else None},
        "holdings": {
            "status": "ready" if holdings_ready else "blocked",
            "portfolio_observed_at": observed_at.isoformat() if isinstance(observed_at, datetime) else None,
            "reference_trade_date": str(reference_trade_date) if reference_trade_date else None,
            "portfolio_trade_date": str(portfolio_trade_date) if portfolio_trade_date else None,
            "sync_mode": "manual",
            "subsequent_trades": "unknown",
            "freshness_status": "current" if portfolio_current else "stale_or_unverified",
            "age_seconds": int(portfolio_age.total_seconds()) if portfolio_age is not None else None,
            "actions": holding_actions,
        },
        "new_buys": {"status": "ready", "actions": new_buy_actions},
        "delivery": {
            "market_eligible": market_available,
            "market_complete": market_complete,
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

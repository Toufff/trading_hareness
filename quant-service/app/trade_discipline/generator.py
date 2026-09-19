"""Pure plan generation: frozen evidence in, one ``DisciplinePlan`` out.

``generate`` performs no I/O.  ``inputs.py`` collects the evidence (read-only
database + the existing quote sources) and hands it over as ``GenerationInputs``
so the same inputs always produce the same plan and the same ``inputs_hash``.

A plan that fails the quality gate is still returned, with
``status="rejected_by_quality"`` and every failing assertion attached.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .contracts import CONTRACT_VERSION, DisciplinePlan, PositionRef
from .quality import evaluate_quality, quality_passed
from .stage import classify_stage, daily_metrics
from .templates import (
    DEFAULT_RISK_PER_TRADE_PCT,
    TEMPLATE_VERSION,
    build_sizing,
    build_template,
    closure_within,
    hard_stop_price,
    new_buy_entry,
)

GENERATOR_VERSION = "trade-discipline-generator-v3"
SHANGHAI = ZoneInfo("Asia/Shanghai")
VALIDITY_TRADING_DAYS = 5
SESSION_CLOSE = time(15, 0)
PLAN_KEY_HASH_CHARS = 12
RESEARCH_CONDITION_NOTE = "研究条件，非系统线"
RECOMMENDATION_TEXT_KEYS = ("priority", "stage", "why_now", "trigger", "invalidation")


class CalendarInfo(BaseModel):
    """Upcoming exchange sessions and the closures between them."""

    upcoming_trading_dates: list[date] = Field(default_factory=list, max_length=40)
    closure_gaps: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class GenerationInputs(BaseModel):
    """Everything a plan is derived from; hashing this gives ``inputs_hash``."""

    run_id: str = Field(min_length=1, max_length=64)
    account_key: str = Field(min_length=1, max_length=64)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=120)
    as_of: datetime
    bars: list[dict[str, Any]] = Field(min_length=1)
    equity: Decimal = Field(gt=0)
    cash: Decimal | None = Field(default=None, ge=0)
    position: dict[str, Any] | None = None
    sector: dict[str, Any] | None = None
    lane: dict[str, Any] | None = None
    calendar: CalendarInfo = Field(default_factory=CalendarInfo)
    previous_plan: dict[str, Any] | None = None
    risk_per_trade_pct: Decimal = Field(default=DEFAULT_RISK_PER_TRADE_PCT, gt=0, le=10)
    lowered_reason: str | None = Field(default=None, max_length=400)
    evidence_refs: list[str] = Field(default_factory=list, max_length=40)
    # The reviewed pool's human wording for this symbol (trigger / invalidation
    # text).  Context for the reader of a new-buy card, never a price source.
    recommendation: dict[str, Any] | None = None
    # Part of the fingerprint on purpose: ``plan_key`` is derived from the
    # inputs hash, so the same evidence run through a different generator
    # version must not collide with the stored plan of the previous version
    # (``persist_plan`` would raise a content conflict instead of superseding).
    generator_version: str = GENERATOR_VERSION

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone offset")
        return value

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         default=str).encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _position_ref(position: dict[str, Any] | None, observed_default: datetime) -> PositionRef | None:
    if not position:
        return None
    quantity = int(position.get("quantity") or 0)
    if quantity <= 0:
        return None
    observed_raw = position.get("observed_at")
    observed = observed_raw if isinstance(observed_raw, datetime) else (
        datetime.fromisoformat(str(observed_raw)) if observed_raw else observed_default)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=SHANGHAI)
    return PositionRef(
        snapshot_id=str(position.get("snapshot_id") or "unknown-snapshot"),
        observed_at=observed, quantity=quantity,
        sellable_quantity=int(position.get("sellable_quantity") if position.get("sellable_quantity") is not None
                              else quantity),
        average_cost=_decimal(position.get("average_cost")),
        market_price=_decimal(position.get("market_price")),
        market_value=_decimal(position.get("market_value")),
    )


def t1_locked_shares_for(position: PositionRef | None, trading_date: date) -> int:
    """Shares the T+1 rule locks on ``trading_date``: ``quantity - sellable`` of a same-day snapshot.

    A snapshot from an earlier day describes a lock that has already lapsed
    (everything it held is sellable by the plan's trading date), so it counts
    as zero rather than as a lock the card would falsely assert.
    """
    if position is None or position.observed_at.astimezone(SHANGHAI).date() != trading_date:
        return 0
    return max(0, position.quantity - position.sellable_quantity)


def recommendation_conditions(recommendation: dict[str, Any] | None) -> dict[str, Any] | None:
    """The pool's trigger/invalidation sentences, labelled as research wording, not system lines."""
    if not isinstance(recommendation, dict):
        return None
    texts = {key: recommendation.get(key) for key in RECOMMENDATION_TEXT_KEYS
             if recommendation.get(key) not in (None, "")}
    if not texts.get("trigger") and not texts.get("invalidation"):
        return None
    return {"decision_id": str(recommendation.get("decision_id") or ""),
            "as_of_date": str(recommendation.get("as_of_date") or ""),
            **{key: str(value) for key, value in texts.items()},
            "note": RESEARCH_CONDITION_NOTE, "evaluable": False}


def _validity(calendar: CalendarInfo, as_of: datetime) -> tuple[datetime, str]:
    upcoming = sorted(calendar.upcoming_trading_dates)
    if not upcoming:
        return as_of, ""
    horizon = upcoming[min(VALIDITY_TRADING_DAYS, len(upcoming)) - 1]
    return datetime.combine(horizon, SESSION_CLOSE, tzinfo=SHANGHAI), horizon.isoformat()


def _evidence_refs(inputs: GenerationInputs, metrics: dict[str, Any],
                   position: PositionRef | None) -> list[str]:
    refs = [f"run:{inputs.run_id}",
            f"bars:{metrics['bars_used']}:{metrics['trading_date']}"]
    if metrics.get("synthetic_bars"):
        refs.append(f"bars_synthetic:{metrics['synthetic_bars']}")
    if position is not None:
        refs.append(f"position_snapshot:{position.snapshot_id}")
    if inputs.sector and inputs.sector.get("code"):
        refs.append(f"sector:{inputs.sector.get('taxonomy') or 'longhu_ths_industry'}:{inputs.sector['code']}")
    if inputs.lane and inputs.lane.get("run_id"):
        refs.append(f"lane_run:{inputs.lane['run_id']}")
    if inputs.previous_plan and inputs.previous_plan.get("plan_id"):
        refs.append(f"supersedes:{inputs.previous_plan['plan_id']}")
    refs.extend(inputs.evidence_refs)
    return refs[:40]


def generate(inputs: GenerationInputs) -> DisciplinePlan:
    """Derive one discipline plan.  Pure: same inputs, same plan."""
    metrics = daily_metrics(inputs.bars)
    decision = classify_stage(metrics, inputs.lane)
    stage = decision["stage"]

    position = _position_ref(inputs.position, inputs.as_of)
    plan_kind = "holding" if position is not None else "new_buy"
    # A new buy is sized on ``entry_price = max(lane.reference, latest close)``:
    # the lane reference is a structure level the stock may already be far
    # above, and a stop/share count taken from it understates the real risk.
    latest_bar = next((row for row in inputs.bars
                       if str(row.get("trading_date"))[:10] == str(metrics["trading_date"])[:10]), {})
    entry = (new_buy_entry(metrics, inputs.lane, last_close_forming=bool(latest_bar.get("forming")))
             if plan_kind == "new_buy" else None)
    reference_price = (Decimal(str(metrics["close"])).quantize(Decimal("0.01"))
                       if entry is None else Decimal(str(entry["entry_price"])).quantize(Decimal("0.01")))

    hard_stop, _ = hard_stop_price(stage, metrics, reference_price)
    sizing = build_sizing(stage=stage, equity=inputs.equity, risk_per_trade_pct=inputs.risk_per_trade_pct,
                          reference_price=reference_price, hard_stop=hard_stop,
                          current_shares=position.quantity if position else 0)

    valid_until, valid_until_limit = _validity(inputs.calendar, inputs.as_of)
    calendar_payload = {
        "closure_gaps": inputs.calendar.closure_gaps,
        "sessions": [str(metrics["trading_date"])[:10],
                     *(day.isoformat() for day in inputs.calendar.upcoming_trading_dates)],
    }
    closure = closure_within(calendar_payload, valid_until.date().isoformat())
    previous = inputs.previous_plan or {}
    sector = inputs.sector or {}
    sector_available = bool(sector.get("code"))

    template = build_template(
        stage, metrics, inputs.position, sizing, calendar_payload,
        plan_kind=plan_kind, lane=inputs.lane, sector_available=sector_available,
        previous_trail=_decimal(previous.get("trail")),
        valid_until_date=valid_until.date().isoformat(), entry=entry,
    )

    trading_date = date.fromisoformat(str(metrics["trading_date"]))
    frozen = dict(metrics)
    frozen.update({
        "reference_price": float(reference_price),
        # T+1: shares bought today cannot be sold today.  A price line drawn on
        # the generation day is executable from the next session on; the card
        # says so instead of implying an immediate exit is possible.  Only a
        # snapshot taken on the plan's own trading date can describe that lock.
        "t1_locked_shares": t1_locked_shares_for(position, trading_date),
        "t1_snapshot_at": position.observed_at.astimezone(SHANGHAI).isoformat() if position is not None else None,
        # Lines the template deliberately did not draw, each with the rule that
        # refused it.  An absent line is never a silent absence.
        "omitted_lines": template.omitted,
        "sector": {"code": sector.get("code"), "name": sector.get("name"),
                   "taxonomy": sector.get("taxonomy") or "longhu_ths_industry"} if sector_available else None,
        "sector_available": sector_available,
        "previous_hard_stop": float(previous["hard_stop"]) if previous.get("hard_stop") is not None else None,
        "previous_trail": float(previous["trail"]) if previous.get("trail") is not None else None,
        "closure_required": closure is not None,
        "closure": closure,
        # The calendar the holiday rule read, frozen so the gate re-derives the
        # requirement from the plan itself instead of trusting the flag above.
        "calendar": calendar_payload,
        "valid_until_limit": valid_until_limit,
        "stage_decision": {"stage": stage, "rule_id": decision["rule_id"], "reason": decision["reason"],
                           "lane": decision["lane"], "evidence": decision["evidence"]},
        "risk_per_trade_pct": float(inputs.risk_per_trade_pct),
    })
    if entry is not None:
        # lane_reference / last_close / entry_price and where each came from,
        # so the sizing basis of a new buy is recomputable from the card.
        frozen["entry"] = entry
        conditions = recommendation_conditions(inputs.recommendation)
        if conditions is not None:
            frozen["recommendation_conditions"] = conditions

    lines = template.lines

    # The key carries the evidence, not only the day: re-running on the same
    # evidence re-derives the same key and stays idempotent, while a run against
    # changed evidence (a moved forming bar, a new snapshot) becomes a new row
    # that supersedes the previous one.  A day-granular key made the documented
    # "改计划 = 新计划 + supersedes_plan_id" path unreachable inside one session.
    fingerprint = inputs.fingerprint()
    plan = DisciplinePlan(
        contract_version=CONTRACT_VERSION,
        plan_key=(f"{inputs.account_key}:{inputs.symbol}:{trading_date.isoformat()}"
                  f":{plan_kind}:{fingerprint[:PLAN_KEY_HASH_CHARS]}"),
        run_id=inputs.run_id, account_key=inputs.account_key, symbol=inputs.symbol, name=inputs.name,
        plan_kind=plan_kind, stage=stage, template_key=f"{stage}@{TEMPLATE_VERSION}",
        template_version=TEMPLATE_VERSION, as_of_at=inputs.as_of, trading_date=trading_date,
        valid_until=valid_until, position=position, metrics=frozen, sizing=sizing, lines=lines,
        evidence_refs=_evidence_refs(inputs, metrics, position),
        quality=[], status="active",
        supersedes_plan_id=previous.get("plan_id"), lowered_reason=inputs.lowered_reason,
        inputs_hash=fingerprint, generator_version=GENERATOR_VERSION,
    )
    checks = evaluate_quality(plan)
    return plan.model_copy(update={"quality": checks,
                                   "status": "active" if quality_passed(checks) else "rejected_by_quality"})


__all__ = ["CalendarInfo", "GENERATOR_VERSION", "GenerationInputs", "PLAN_KEY_HASH_CHARS",
           "RESEARCH_CONDITION_NOTE", "SESSION_CLOSE", "VALIDITY_TRADING_DAYS", "generate",
           "recommendation_conditions", "t1_locked_shares_for"]

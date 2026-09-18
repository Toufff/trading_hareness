"""Pure evaluation of a stored plan against later tape.

A plan states what would have to happen; this module states what did happen,
without deciding anything.  It takes the frozen ``DisciplinePlan`` plus the
daily bars and/or the minute tape observed afterwards and returns one
``Evaluation`` with a ``LineState`` per line, so an evaluation row can be
recomputed months later from the same inputs.

Three rules shape it:

* a price line only triggers on the basis it declared (``confirm.basis``) and
  only after ``confirm.bars`` consecutive qualifying observations;
* a line whose ``extra`` conditions cannot be computed from the supplied rows
  stays ``armed`` with the reason recorded -- it never silently fails to fire;
* ``execute_by="time"`` lines (exposure / holiday / time_stop) are never
  compared against a price.  They are reported as due or not due against the
  exchange calendar, with ``basis="time"``.

No database, no provider, no clock: ``as_of`` is an input.
"""

from __future__ import annotations

import hashlib
import json
import operator
from datetime import date, datetime, time
from decimal import Decimal
from statistics import fmean
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .contracts import DisciplinePlan, Evaluation, Line, LineState
from .generator import CalendarInfo
from .stage import normalize_bars

EVALUATOR_VERSION = "trade-discipline-evaluator-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")

SESSION_CLOSE = time(15, 0)
EXPOSURE_ACTION_TIME = time(9, 45)        # "next_open+15m"
BEFORE_CLOSE_ACTION_TIME = time(14, 55)   # "..._before_close"

VOLUME_EXPAND_MULTIPLE = 1.5
VOLUME_CONTRACT_MULTIPLE = 0.7
VOLUME_MEAN_WINDOW = 5
CLIMAX_WINDOW = 20

EXIT_ACTIONS = frozenset({"exit_all"})
REDUCE_ACTIONS = frozenset({"reduce_to_shares", "reduce_by_pct"})
SELL_ACTIONS = EXIT_ACTIONS | REDUCE_ACTIONS
BUY_ACTIONS = frozenset({"buy_up_to_shares"})

_COMPARISONS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_DAILY_METRIC_KEYS = {"daily_close": "close", "last": "close", "low": "low", "high": "high"}


class EvaluationInputs(BaseModel):
    """Everything an evaluation reads.  Hashing it gives ``inputs_hash``."""

    plan_id: str = Field(min_length=1, max_length=200)
    as_of: datetime
    basis: Literal["daily", "minute"] = "daily"
    bars: list[dict[str, Any]] = Field(default_factory=list, max_length=400)
    minutes: list[dict[str, Any]] = Field(default_factory=list, max_length=600)
    calendar: CalendarInfo = Field(default_factory=CalendarInfo)
    sector_change_pct: float | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=40)

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


class Observation(BaseModel):
    """One comparable tape point: when it happened and what the metric read."""

    at: datetime
    value: float
    basis: Literal["daily", "minute"]
    label: str = ""
    below_vwap: bool | None = None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _minute_at(row: dict[str, Any], fallback_day: date) -> datetime | None:
    day = _as_date(row.get("session_date") or row.get("trading_date")) or fallback_day
    raw = str(row.get("time") or row.get("minute") or "").replace(":", "").strip()
    if len(raw) < 4 or not raw[:4].isdigit():
        return None
    return datetime.combine(day, time(int(raw[:2]), int(raw[2:4])), tzinfo=SHANGHAI)


def extra_conditions(bars: list[dict[str, Any]], minutes: list[dict[str, Any]],
                     sector_change_pct: float | None) -> dict[str, bool | None]:
    """Compute every ``ExtraCondition`` from the supplied rows.

    ``None`` means "not computable from these rows" and is never coerced to
    ``False``: a line gated on an unavailable condition stays armed instead of
    quietly reporting that it did not fire.
    """
    rows = normalize_bars(bars)
    today = rows[-1] if rows else None
    previous = rows[-2] if len(rows) >= 2 else None
    volumes = [row["volume"] for row in rows if row["volume"] is not None]
    prior_mean = (fmean(volumes[-(VOLUME_MEAN_WINDOW + 1):-1])
                  if len(volumes) >= VOLUME_MEAN_WINDOW + 1 and rows and rows[-1]["volume"] is not None else None)
    max_volume = max(volumes[-CLIMAX_WINDOW:]) if volumes else None
    today_volume = today["volume"] if today else None
    today_amount = today["amount"] if today else None
    prev_amount = previous["amount"] if previous else None

    climax: bool | None = None
    if today is not None and today_volume is not None and max_volume is not None:
        span = today["high"] - today["low"]
        lower_half = today["close"] <= today["low"] + span / 2
        climax = today_volume >= max_volume and lower_half

    vwap_rows = [row for row in minutes if _number(row.get("vwap")) and _number(row.get("close")) is not None]
    last_minute = vwap_rows[-1] if vwap_rows else None
    below_vwap: bool | None = None
    if last_minute is not None:
        below_vwap = float(last_minute["close"]) < float(last_minute["vwap"])

    return {
        "sector_change_negative": None if sector_change_pct is None else sector_change_pct < 0,
        "sector_not_weak": None if sector_change_pct is None else sector_change_pct >= 0,
        "amount_ge_prev_day": (None if today_amount is None or prev_amount is None
                               else today_amount >= prev_amount),
        "volume_expand_1_5x": (None if today_volume is None or prior_mean is None
                               else today_volume >= VOLUME_EXPAND_MULTIPLE * prior_mean),
        "volume_contract_0_7x": (None if today_volume is None or prior_mean is None
                                 else today_volume <= VOLUME_CONTRACT_MULTIPLE * prior_mean),
        "below_vwap": below_vwap,
        "after_volume_climax": climax,
    }


def daily_observations(plan: DisciplinePlan, bars: list[dict[str, Any]], as_of: datetime,
                       metric: str) -> list[Observation]:
    """Closed daily sessions after the plan date, up to ``as_of``.

    A session only counts once its 15:00 close has actually happened, so an
    intraday run never evaluates a daily line against an unfinished bar.
    """
    key = _DAILY_METRIC_KEYS.get(metric)
    if key is None:
        return []
    out: list[Observation] = []
    for row in normalize_bars(bars):
        day = _as_date(row["trading_date"])
        if day is None or day <= plan.trading_date:
            continue
        stamped = datetime.combine(day, SESSION_CLOSE, tzinfo=SHANGHAI)
        if stamped > as_of:
            continue
        out.append(Observation(at=stamped, value=float(row[key]), basis="daily", label=day.isoformat()))
    return out


def minute_observations(minutes: list[dict[str, Any]], as_of: datetime, metric: str) -> list[Observation]:
    """Completed minutes up to ``as_of``; the in-progress minute never confirms."""
    if metric not in {"minute_close", "last", "vwap"}:
        return []
    fallback = as_of.astimezone(SHANGHAI).date()
    out: list[Observation] = []
    for row in minutes:
        stamped = _minute_at(row, fallback)
        close = _number(row.get("close"))
        vwap = _number(row.get("vwap"))
        value = vwap if metric == "vwap" else close
        if stamped is None or value is None or stamped > as_of or row.get("is_complete") is False:
            continue
        out.append(Observation(at=stamped, value=float(value), basis="minute",
                               label=stamped.strftime("%H:%M"),
                               below_vwap=None if close is None or vwap is None else close < vwap))
    out.sort(key=lambda item: item.at)
    return out


def _threshold(plan: DisciplinePlan, line: Line) -> tuple[float | None, str]:
    if line.price is not None:
        return float(line.price), "line.price"
    if line.pct is not None:
        reference = plan.sizing.reference_price if plan.sizing else plan.metrics.get("reference_price")
        if reference is None:
            return None, "pct_without_reference_price"
        return float(Decimal(str(reference)) * (Decimal("1") + line.pct / Decimal("100"))), "pct_of_reference_price"
    return None, "no_price_or_pct"


def _extras_verdict(line: Line, conditions: dict[str, bool | None]) -> tuple[list[str], list[str]]:
    """Return ``(unavailable, unmet)`` extra condition names for one line."""
    unavailable = [name for name in line.extra if conditions.get(name) is None]
    unmet = [name for name in line.extra if conditions.get(name) is False]
    return unavailable, unmet


def _first_streak(observations: list[Observation], op: str, threshold: float, bars: int,
                  per_observation_extra: str | None) -> tuple[Observation | None, int]:
    """First observation at which ``bars`` consecutive qualifying points are complete."""
    compare = _COMPARISONS[op]
    streak = 0
    best = 0
    for observation in observations:
        hit = compare(observation.value, threshold)
        if hit and per_observation_extra == "below_vwap":
            hit = observation.below_vwap is True
        if hit:
            streak += 1
            best = max(best, streak)
            if streak >= bars:
                return observation, best
        else:
            streak = 0
    return None, best


def _trading_dates(plan: DisciplinePlan, calendar: CalendarInfo) -> list[date]:
    """Plan session first, then every upcoming session, de-duplicated and ordered."""
    ordered = sorted({plan.trading_date, *calendar.upcoming_trading_dates})
    return [day for day in ordered if day >= plan.trading_date]


def _time_deadline(plan: DisciplinePlan, line: Line, calendar: CalendarInfo) -> tuple[datetime | None, str]:
    upcoming = [day for day in sorted(calendar.upcoming_trading_dates) if day > plan.trading_date]
    if line.kind == "exposure":
        if not upcoming:
            return None, "calendar_has_no_upcoming_session"
        return datetime.combine(upcoming[0], EXPOSURE_ACTION_TIME, tzinfo=SHANGHAI), "next_open+15m"
    if line.kind == "holiday":
        day = _as_date((line.execute_at or "").split("_")[0])
        if day is None:
            return None, "holiday_line_without_a_dated_execute_at"
        return datetime.combine(day, BEFORE_CLOSE_ACTION_TIME, tzinfo=SHANGHAI), f"{day.isoformat()}_before_close"
    if line.kind == "time_stop":
        days = line.trading_days or 0
        if days <= 0 or len(upcoming) < days:
            return None, "calendar_has_fewer_sessions_than_the_time_stop"
        return datetime.combine(upcoming[days - 1], SESSION_CLOSE, tzinfo=SHANGHAI), f"T+{days}_close"
    if line.execute_at:
        day = _as_date(line.execute_at.split("_")[0])
        if day is not None:
            return datetime.combine(day, SESSION_CLOSE, tzinfo=SHANGHAI), line.execute_at
    return None, "execute_at_is_not_a_dated_deadline"


def _time_line_state(plan: DisciplinePlan, line: Line, inputs: EvaluationInputs,
                     bars: list[dict[str, Any]]) -> LineState:
    """A time line is due or not due; it is never compared against a price."""
    deadline, note = _time_deadline(plan, line, inputs.calendar)
    evidence: dict[str, Any] = {"execute_by": "time", "execute_at": line.execute_at, "rule": note,
                                "deadline": deadline.isoformat() if deadline else None,
                                "as_of": inputs.as_of.isoformat()}
    if line.kind == "time_stop" and line.price is not None:
        reclaim = [observation for observation in daily_observations(plan, bars, inputs.as_of, "daily_close")
                   if observation.value >= float(line.price)]
        evidence["confirm_price"] = float(line.price)
        if reclaim:
            evidence.update({"reclaimed_on": reclaim[0].label, "reclaim_close": reclaim[0].value, "due": False})
            return LineState(kind=line.kind, label=line.label, state="cancelled", basis="time", evidence=evidence)
        closes = daily_observations(plan, bars, inputs.as_of, "daily_close")
        evidence["sessions_observed"] = [observation.label for observation in closes]
        evidence["last_close"] = closes[-1].value if closes else None
    if deadline is None:
        evidence["due"] = None
        return LineState(kind=line.kind, label=line.label, state="armed", basis="time", evidence=evidence)
    due = inputs.as_of >= deadline
    evidence["due"] = due
    if due:
        return LineState(kind=line.kind, label=line.label, state="triggered", triggered_at=deadline,
                         basis="time", evidence=evidence)
    sessions = [day for day in _trading_dates(plan, inputs.calendar)
                if day > inputs.as_of.astimezone(SHANGHAI).date() and day <= deadline.date()]
    evidence["trading_days_remaining"] = len(sessions)
    return LineState(kind=line.kind, label=line.label, state="armed", basis="time", evidence=evidence)


def _price_line_state(plan: DisciplinePlan, line: Line, inputs: EvaluationInputs,
                      conditions: dict[str, bool | None]) -> LineState:
    basis = line.confirm.basis
    evidence: dict[str, Any] = {"metric": line.metric, "op": line.op, "confirm_bars": line.confirm.bars,
                                "extra": list(line.extra), "as_of": inputs.as_of.isoformat()}
    if basis != inputs.basis:
        evidence["not_evaluated"] = f"line confirms on {basis} but this run is {inputs.basis}"
        return LineState(kind=line.kind, label=line.label, state="armed", basis=basis, evidence=evidence)

    threshold, source = _threshold(plan, line)
    evidence["threshold_source"] = source
    if threshold is None or line.metric is None or line.op is None:
        evidence["not_evaluated"] = "line has no evaluable metric/op/threshold"
        return LineState(kind=line.kind, label=line.label, state="armed", basis=basis, evidence=evidence)
    evidence["threshold"] = threshold

    observations = (daily_observations(plan, inputs.bars, inputs.as_of, line.metric) if basis == "daily"
                    else minute_observations(inputs.minutes, inputs.as_of, line.metric))
    evidence["observations"] = len(observations)
    if observations:
        evidence["last_observation"] = {"at": observations[-1].at.isoformat(), "value": observations[-1].value}
    if not observations:
        evidence["not_evaluated"] = f"no closed {basis} observation for metric {line.metric} after the plan session"
        return LineState(kind=line.kind, label=line.label, state="armed", basis=basis, evidence=evidence)

    # ``below_vwap`` is the one condition that is meaningful per minute; the
    # others describe the whole session and are read from the daily rows.
    per_observation = "below_vwap" if basis == "minute" and "below_vwap" in line.extra else None
    checkable = [name for name in line.extra if name != per_observation]
    unavailable, unmet = _extras_verdict(line.model_copy(update={"extra": checkable}), conditions)
    if per_observation and all(observation.below_vwap is None for observation in observations):
        unavailable = [*unavailable, per_observation]
    evidence["extra_values"] = {name: conditions.get(name) for name in line.extra}
    if unavailable:
        evidence["not_evaluated"] = f"extra conditions not computable from the supplied rows: {unavailable}"
        return LineState(kind=line.kind, label=line.label, state="armed", basis=basis, evidence=evidence)

    hit, longest = _first_streak(observations, line.op, threshold, line.confirm.bars, per_observation)
    evidence["longest_streak"] = longest
    if hit is None or unmet:
        if unmet:
            evidence["extra_unmet"] = unmet
        return LineState(kind=line.kind, label=line.label, state="armed", basis=basis, evidence=evidence)
    evidence["confirmed_at"] = hit.label
    return LineState(kind=line.kind, label=line.label, state="triggered", triggered_at=hit.at,
                     trigger_price=Decimal(str(round(hit.value, 4))), basis=basis, evidence=evidence)


def _signal(state: LineState, line: Line) -> str | None:
    if state.state != "triggered":
        return None
    if line.action.type in EXIT_ACTIONS:
        return "exit_signalled"
    if line.action.type == "reduce_to_shares" and (line.action.value or 0) == 0:
        return "exit_signalled"   # reducing to zero shares is an exit, whatever the action name says
    if line.action.type in REDUCE_ACTIONS:
        return "reduce_signalled"
    return None


def evaluate(plan: DisciplinePlan, inputs: EvaluationInputs) -> Evaluation:
    """Evaluate every line of ``plan`` against the supplied tape.  Pure."""
    as_of_date = inputs.as_of.astimezone(SHANGHAI).date()
    conditions = extra_conditions(inputs.bars, inputs.minutes, inputs.sector_change_pct)
    fingerprint = hashlib.sha256(
        f"{plan.plan_key}|{plan.inputs_hash}|{inputs.fingerprint()}".encode("utf-8")).hexdigest()

    if plan.status != "active":
        states = [LineState(kind=line.kind, label=line.label, state="cancelled", basis=line.confirm.basis,
                            evidence={"plan_status": plan.status}) for line in plan.lines]
        return Evaluation(plan_id=inputs.plan_id, as_of_at=inputs.as_of, trading_date=as_of_date,
                          basis=inputs.basis, line_states=states, plan_state="expired",
                          inputs_hash=fingerprint)

    states: list[LineState] = []
    for line in plan.lines:
        if line.execute_by == "time":
            states.append(_time_line_state(plan, line, inputs, inputs.bars))
        else:
            states.append(_price_line_state(plan, line, inputs, conditions))

    expired = inputs.as_of > plan.valid_until
    cancelled = any(state.state == "triggered" and line.kind == "cancel"
                    for line, state in zip(plan.lines, states, strict=True))
    signals = {signal for line, state in zip(plan.lines, states, strict=True)
               if (signal := _signal(state, line)) is not None}

    final: list[LineState] = []
    for state in states:
        if state.state == "armed" and cancelled:
            final.append(state.model_copy(update={"state": "cancelled",
                                                  "evidence": {**state.evidence, "voided_by": "cancel"}}))
        elif state.state == "armed" and expired:
            final.append(state.model_copy(update={"state": "expired",
                                                  "evidence": {**state.evidence,
                                                               "valid_until": plan.valid_until.isoformat()}}))
        else:
            final.append(state)

    # A voided plan issues no instruction: a ``cancel`` outranks any exit or
    # reduce that the same tape produced, because the entry it protected is off.
    if cancelled:
        plan_state = "expired"
    elif "exit_signalled" in signals:
        plan_state = "exit_signalled"
    elif "reduce_signalled" in signals:
        plan_state = "reduce_signalled"
    elif expired:
        plan_state = "expired"
    else:
        plan_state = "active"
    return Evaluation(plan_id=inputs.plan_id, as_of_at=inputs.as_of, trading_date=as_of_date,
                      basis=inputs.basis, line_states=final, plan_state=plan_state, inputs_hash=fingerprint)


__all__ = [
    "BUY_ACTIONS", "CLIMAX_WINDOW", "EVALUATOR_VERSION", "EXIT_ACTIONS", "EXPOSURE_ACTION_TIME",
    "EvaluationInputs", "Observation", "REDUCE_ACTIONS", "SELL_ACTIONS", "SESSION_CLOSE",
    "VOLUME_CONTRACT_MULTIPLE", "VOLUME_EXPAND_MULTIPLE", "daily_observations", "evaluate",
    "extra_conditions", "minute_observations",
]

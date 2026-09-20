"""Fail-closed minute-tape validation and discipline evaluation assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import DisciplinePlan, Evaluation, Line, LineState
from .evaluator import EvaluationInputs, evaluate
from .inputs import (
    forming_bar, merge_forming_bar, sector_daily_change, sector_membership,
    settled_bar_for, settled_daily_bars, trading_calendar,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_COMPLETED_MINUTE_AGE = timedelta(seconds=150)


@dataclass(frozen=True)
class ValidatedMinuteTape:
    session_date: date
    as_of: datetime
    rows: tuple[dict[str, Any], ...]


class MinuteTapeRejected(ValueError):
    """The vendor tape cannot safely prove consecutive-minute conditions."""


def line_key(index: int, line: Line) -> str:
    body = json.dumps(line.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return f"{index}:{line.kind}:{sha256(body.encode('utf-8')).hexdigest()[:24]}"


def _stamp(day: date, row: dict[str, Any]) -> datetime | None:
    raw = str(row.get("time") or row.get("minute") or "").replace(":", "")[:4]
    if len(raw) != 4 or not raw.isdigit():
        return None
    hour, minute = int(raw[:2]), int(raw[2:])
    try:
        return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)
    except ValueError:
        return None


def validate_minute_tape(session: dict[str, Any], now: datetime,
                         *, max_age: timedelta = MAX_COMPLETED_MINUTE_AGE) -> ValidatedMinuteTape:
    """Require the declared current session, completed rows and no hidden gaps.

    Missing a minute can turn two non-consecutive threshold hits into a false
    streak.  The whole tape therefore fails closed when completed rows are
    duplicated, out of order or discontinuous (apart from the lunch recess).
    """
    local_now = now.astimezone(SHANGHAI)
    try:
        day = date.fromisoformat(str(session.get("session_date") or ""))
    except ValueError as error:
        raise MinuteTapeRejected("minute_session_date_missing") from error
    if day != local_now.date():
        raise MinuteTapeRejected(f"minute_session_wrong_date:{day.isoformat()}")
    completed: list[tuple[datetime, dict[str, Any]]] = []
    declared_dates: set[str] = set()
    for source in session.get("rows") or []:
        if not isinstance(source, dict):
            continue
        if source.get("session_date"):
            declared_dates.add(str(source["session_date"]))
        if source.get("is_complete") is False:
            continue
        stamped = _stamp(day, source)
        if stamped is None or source.get("close") is None:
            raise MinuteTapeRejected("minute_row_unusable")
        completed.append((stamped, dict(source)))
    if declared_dates and declared_dates != {day.isoformat()}:
        raise MinuteTapeRejected("minute_rows_cross_session")
    if not completed:
        raise MinuteTapeRejected("minute_no_completed_rows")
    observed_order = [item[0] for item in completed]
    if observed_order != sorted(observed_order) or len(observed_order) != len(set(observed_order)):
        raise MinuteTapeRejected("minute_rows_not_strictly_ordered")
    for previous, current in zip(observed_order, observed_order[1:]):
        delta = current - previous
        # Longhu's current contract resumes its completed tape at 13:01 after
        # 11:30 (the 13:00 row remains the provider's incomplete head).  Older
        # captures may contain a completed 13:00 row, so both explicit recess
        # transitions are accepted; no other multi-minute gap is.
        lunch = previous.time() == time(11, 30) and current.time() in {time(13, 0), time(13, 1)}
        if delta != timedelta(minutes=1) and not lunch:
            raise MinuteTapeRejected(
                f"minute_gap:{previous.strftime('%H%M')}->{current.strftime('%H%M')}")
    latest = observed_order[-1]
    age = local_now - latest
    if age < timedelta(0) or age > max_age:
        raise MinuteTapeRejected(f"minute_tape_stale:{int(age.total_seconds())}s")
    return ValidatedMinuteTape(day, latest, tuple(row for _stamped, row in completed))


def _forming_from_tape(tape: ValidatedMinuteTape) -> dict[str, Any] | None:
    rows = list(tape.rows)
    closes = [float(row["close"]) for row in rows]
    last = rows[-1]
    summary = {
        "last": closes[-1], "open": closes[0], "high": max(closes), "low": min(closes),
        "vwap": last.get("vwap"),
    }
    quote = {
        "price": closes[-1], "cumulative_volume_lot": last.get("cumulative_volume_lot"),
        "cumulative_amount": last.get("cumulative_amount"),
    }
    return forming_bar(tape.session_date, quote, summary)


def evaluation_context(connection: Any, *, plan_id: str, plan: DisciplinePlan,
                       tape: ValidatedMinuteTape) -> EvaluationInputs:
    """Freeze the exact daily/minute/calendar evidence for one alert check."""
    settled = settled_daily_bars(connection, plan.symbol, tape.session_date + timedelta(days=1))
    forming = _forming_from_tape(tape)
    bars, _basis = merge_forming_bar(settled, forming, settled_day=tape.session_date)
    calendar, day_is_open = trading_calendar(connection, tape.session_date)
    if not day_is_open:
        raise MinuteTapeRejected("calendar_marks_session_closed")
    return EvaluationInputs(
        plan_id=plan_id, as_of=tape.as_of, basis="minute", bars=bars,
        minutes=list(tape.rows), calendar=calendar, sector_change_pct=None,
        evidence_refs=[
            f"longhu_minute_session:{tape.session_date.isoformat()}",
            f"longhu_last_completed_minute:{tape.as_of.isoformat()}",
        ],
    )


def evaluate_minute_plan(connection: Any, *, plan_id: str, plan: DisciplinePlan,
                         tape: ValidatedMinuteTape) -> Evaluation:
    return evaluate(plan, evaluation_context(connection, plan_id=plan_id, plan=plan, tape=tape))


def evaluate_daily_plan(connection: Any, *, plan_id: str, plan: DisciplinePlan,
                        as_of: datetime) -> Evaluation:
    """Evaluate settled daily lines only after today's canonical bar exists."""
    day = as_of.astimezone(SHANGHAI).date()
    bars = settled_daily_bars(connection, plan.symbol, day + timedelta(days=1))
    if settled_bar_for(bars, day) is None:
        raise MinuteTapeRejected("authoritative_daily_bar_not_ready")
    calendar, day_is_open = trading_calendar(connection, day)
    if not day_is_open:
        raise MinuteTapeRejected("calendar_marks_session_closed")
    membership = sector_membership(connection, plan.symbol, day, as_of)
    sector_change = (sector_daily_change(
        connection, membership["taxonomy"], membership["code"], day,
    ) if membership else None)
    inputs = EvaluationInputs(
        plan_id=plan_id, as_of=as_of, basis="daily", bars=bars, minutes=[],
        calendar=calendar, sector_change_pct=sector_change,
        evidence_refs=[f"canonical_daily_bar:{day.isoformat()}"],
    )
    return evaluate(plan, inputs)


def basis_line_states(plan: DisciplinePlan, evaluation: Evaluation) -> list[tuple[int, Line, LineState]]:
    return [
        (index, line, state)
        for index, (line, state) in enumerate(zip(plan.lines, evaluation.line_states, strict=True))
        if line.execute_by == "price" and line.confirm.basis == evaluation.basis
    ]


__all__ = [
    "MAX_COMPLETED_MINUTE_AGE", "MinuteTapeRejected", "ValidatedMinuteTape", "evaluate_daily_plan", "evaluate_minute_plan",
    "basis_line_states", "evaluation_context", "line_key", "validate_minute_tape",
]

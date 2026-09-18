"""Pure reconciliation: what the plan signalled versus what was actually traded.

Input is the frozen plan, the evaluations already produced for it and the broker
trade rows (``quant.broker_trade_records`` shape).  Output is one
``ComplianceRecord`` per judgement, with the six verdicts of the contract:

``followed``    a same-direction fill within one trading day of the trigger
``late``        the same fill, but more than one trading day later
``missed``      the line triggered and nothing was filled before ``valid_until``
``early``       a sell no triggered line accounts for -- either nothing had
                triggered yet, or the triggered lines' quantity was already filled
``against_plan`` a buy while a ``no_add`` block was in effect
``unplanned``   a fill this plan never covered (other symbol, outside the window)

``deviation`` always carries the three differences a review asks for: price,
time and quantity.  Nothing here reads a database, a clock or a provider.
"""

from __future__ import annotations

import operator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .contracts import ComplianceRecord, DisciplinePlan, Evaluation, Line, LineState
from .evaluator import BUY_ACTIONS, SELL_ACTIONS, SESSION_CLOSE
from .templates import LOT_SIZE

RECONCILER_VERSION = "trade-discipline-reconcile-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
FOLLOW_WINDOW_TRADING_DAYS = 1

_COMPARISONS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
VERDICTS = ("followed", "early", "late", "missed", "against_plan", "unplanned")


class TradeRecord(BaseModel):
    """One broker fill, in the shape ``quant.broker_trade_records`` stores."""

    trade_record_id: str = Field(min_length=1, max_length=200)
    trade_date: date
    trade_time: time | None = None
    symbol: str = Field(min_length=1, max_length=20)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    name: str = ""

    @field_validator("trade_time", mode="before")
    @classmethod
    def parse_time(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip():
            return time.fromisoformat(value.strip())
        return value

    @property
    def filled_at(self) -> datetime:
        return datetime.combine(self.trade_date, self.trade_time or SESSION_CLOSE, tzinfo=SHANGHAI)

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.trade_date.isoformat(), (self.trade_time or SESSION_CLOSE).isoformat(), self.trade_record_id)


class Signal(BaseModel):
    """A triggered line translated into the fill it expects."""

    line_kind: str
    label: str
    side: Literal["buy", "sell"] | None
    triggered_at: datetime
    line_price: Decimal | None = None
    trigger_price: Decimal | None = None
    expected_quantity: int | None = None
    priority: int = 0


def _as_trades(rows: list[Any]) -> list[TradeRecord]:
    return [row if isinstance(row, TradeRecord) else TradeRecord(**row) for row in rows]


def _sessions(plan: DisciplinePlan, calendar: Any) -> dict[date, int]:
    upcoming = list(getattr(calendar, "upcoming_trading_dates", None) or [])
    ordered = sorted({plan.trading_date, *upcoming})
    return {day: index for index, day in enumerate(ordered)}


def _trading_days_between(sessions: dict[date, int], start: date, end: date) -> tuple[int, str]:
    if start in sessions and end in sessions:
        return sessions[end] - sessions[start], "exchange_calendar"
    return (end - start).days, "calendar_days_fallback"


def _expected_quantity(plan: DisciplinePlan, line: Line) -> int | None:
    held = plan.position.quantity if plan.position else None
    value = line.action.value
    if line.action.type == "exit_all":
        return held
    if line.action.type == "reduce_to_shares" and value is not None and held is not None:
        return max(0, held - int(value))
    if line.action.type == "reduce_by_pct" and value is not None and held is not None:
        return int(held * Decimal(str(value)) / Decimal("100") / LOT_SIZE) * LOT_SIZE
    if line.action.type == "buy_up_to_shares" and value is not None:
        return int(value)
    return None


def _side(line: Line) -> Literal["buy", "sell"] | None:
    if line.action.type in SELL_ACTIONS:
        return "sell"
    if line.action.type in BUY_ACTIONS:
        return "buy"
    return None


def signals_from(plan: DisciplinePlan, evaluations: list[Evaluation]) -> list[Signal]:
    """Earliest trigger per distinct line, translated into an expected fill.

    The daily hard stop and its 3-minute copy carry the same kind, price and
    direction, so they collapse into one expected fill instead of demanding two.
    """
    by_key: dict[tuple[str, str], Line] = {(line.kind, line.label): line for line in plan.lines}
    earliest: dict[tuple[str, str, str | None], Signal] = {}
    for evaluation in sorted(evaluations, key=lambda item: item.as_of_at):
        for state in evaluation.line_states:
            line = by_key.get((state.kind, state.label))
            if line is None or state.state != "triggered":
                continue
            key = (state.kind, str(line.price), _side(line))
            if key in earliest:
                continue
            earliest[key] = Signal(
                line_kind=state.kind, label=state.label, side=_side(line),
                triggered_at=state.triggered_at or evaluation.as_of_at,
                line_price=line.price, trigger_price=state.trigger_price,
                expected_quantity=_expected_quantity(plan, line), priority=line.priority)
    return sorted(earliest.values(), key=lambda item: (item.triggered_at, item.priority, item.line_kind))


def no_add_release(plan: DisciplinePlan, evaluations: list[Evaluation]) -> date | None:
    """First session an evaluation showed the ``no_add`` block lifted.

    A stop that fired once stays ``triggered`` forever, which is right for an
    append-only log but wrong for a standing prohibition.  The block is in force
    only while the latest observation still satisfies the line, so the release is
    read from the recorded ``last_observation`` rather than from the state word.
    """
    lines = plan.lines_of("no_add")
    if not lines:
        return None
    compare = _COMPARISONS.get(lines[0].op or "<")
    for evaluation in sorted(evaluations, key=lambda item: item.as_of_at):
        states: list[LineState] = [state for state in evaluation.line_states if state.kind == "no_add"]
        for state in states:
            if state.state in {"cancelled", "expired"}:
                return evaluation.trading_date
            observation = state.evidence.get("last_observation") or {}
            threshold = state.evidence.get("threshold")
            value = observation.get("value")
            if compare is None or value is None or threshold is None:
                continue
            if not compare(float(value), float(threshold)):
                return evaluation.trading_date
    return None


def _deviation(*, trade: TradeRecord | None, signal: Signal | None, sessions: dict[date, int],
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    expected_price = None
    if signal is not None:
        expected_price = signal.line_price if signal.line_price is not None else signal.trigger_price
    actual_price = trade.price if trade is not None else None
    price_diff = (float(actual_price - expected_price)
                  if actual_price is not None and expected_price is not None else None)
    days = basis = None
    if trade is not None and signal is not None:
        days, basis = _trading_days_between(sessions, signal.triggered_at.astimezone(SHANGHAI).date(),
                                            trade.trade_date)
    expected_quantity = signal.expected_quantity if signal is not None else None
    actual_quantity = trade.quantity if trade is not None else 0
    payload: dict[str, Any] = {
        "line_price": float(signal.line_price) if signal and signal.line_price is not None else None,
        "trigger_price": float(signal.trigger_price) if signal and signal.trigger_price is not None else None,
        "price_expected": float(expected_price) if expected_price is not None else None,
        "price_actual": float(actual_price) if actual_price is not None else None,
        "price_diff": None if price_diff is None else round(price_diff, 4),
        "price_diff_pct": (None if price_diff is None or not expected_price
                           else round(price_diff / float(expected_price) * 100, 4)),
        "time_expected": signal.triggered_at.isoformat() if signal is not None else None,
        "time_actual": trade.filled_at.isoformat() if trade is not None else None,
        "trading_days_diff": days,
        "trading_days_basis": basis,
        "quantity_expected": expected_quantity,
        "quantity_actual": actual_quantity,
        "quantity_diff": None if expected_quantity is None else actual_quantity - expected_quantity,
    }
    if extra:
        payload.update(extra)
    return payload


def _distance_to_lines(plan: DisciplinePlan, trade: TradeRecord) -> dict[str, float]:
    return {f"{line.kind}:{line.price}": round(float(trade.price - line.price), 4)
            for line in plan.lines if line.price is not None}


def reconcile(plan: DisciplinePlan, evaluations: list[Evaluation], trades: list[Any], *,
              as_of: datetime, calendar: Any = None, plan_id: str | None = None) -> list[ComplianceRecord]:
    """Judge every fill against the plan.  Pure: same inputs, same records."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone offset")
    resolved_plan_id = plan_id or (evaluations[0].plan_id if evaluations else plan.plan_key)
    sessions = _sessions(plan, calendar)
    window_end = plan.valid_until.astimezone(SHANGHAI).date()
    rows = sorted(_as_trades(trades), key=lambda item: item.sort_key)

    records: list[ComplianceRecord] = []
    in_window: list[TradeRecord] = []
    for trade in rows:
        if trade.symbol != plan.symbol:
            records.append(ComplianceRecord(
                plan_id=resolved_plan_id, trade_record_id=trade.trade_record_id, line_kind=None,
                verdict="unplanned", deviation=_deviation(trade=trade, signal=None, sessions=sessions),
                notes=f"{trade.symbol} 不在本计划覆盖范围（计划标的 {plan.symbol}）"))
        elif not plan.trading_date <= trade.trade_date <= window_end:
            records.append(ComplianceRecord(
                plan_id=resolved_plan_id, trade_record_id=trade.trade_record_id, line_kind=None,
                verdict="unplanned", deviation=_deviation(trade=trade, signal=None, sessions=sessions),
                notes=(f"成交日 {trade.trade_date.isoformat()} 不在计划有效期 "
                       f"[{plan.trading_date.isoformat()}, {window_end.isoformat()}] 内")))
        else:
            in_window.append(trade)

    # A signal is matched against every same-direction fill from the trigger on,
    # until the quantity it expects is filled: a stop-out taken in two tranches
    # is one obeyed instruction, not one obedience plus one violation.  A fill is
    # never consumed exclusively, so one full exit still satisfies the hard stop,
    # the soft stop and the exposure cut at once, and ``quantity_diff`` carries
    # the running difference against what the line asked for.
    signals = signals_from(plan, evaluations)
    consumed: set[str] = set()
    for signal in signals:
        if signal.side is None:
            continue
        trigger_date = signal.triggered_at.astimezone(SHANGHAI).date()
        candidates = [trade for trade in in_window
                      if trade.side == signal.side and trade.trade_date >= trigger_date]
        if not candidates:
            if as_of >= plan.valid_until:
                records.append(ComplianceRecord(
                    plan_id=resolved_plan_id, trade_record_id=None, line_kind=signal.line_kind,
                    verdict="missed", deviation=_deviation(trade=None, signal=signal, sessions=sessions),
                    notes=(f"{signal.line_kind} 于 {signal.triggered_at.isoformat()} 触发，"
                           f"直到有效期 {plan.valid_until.isoformat()} 仍无{signal.side}成交")))
            continue
        expected = signal.expected_quantity
        filled = 0
        for tranche, trade in enumerate(candidates, start=1):
            consumed.add(trade.trade_record_id)
            filled += trade.quantity
            days, _ = _trading_days_between(sessions, trigger_date, trade.trade_date)
            verdict = "followed" if days <= FOLLOW_WINDOW_TRADING_DAYS else "late"
            tranche_note = f"（第{tranche}笔，累计{filled}股）" if len(candidates) > 1 else ""
            records.append(ComplianceRecord(
                plan_id=resolved_plan_id, trade_record_id=trade.trade_record_id, line_kind=signal.line_kind,
                verdict=verdict,
                deviation=_deviation(trade=trade, signal=signal, sessions=sessions,
                                     extra={"tranche": tranche, "quantity_filled_cumulative": filled,
                                            "quantity_diff": None if expected is None else filled - expected}),
                notes=(f"{signal.line_kind} 于 {signal.triggered_at.isoformat()} 触发，"
                       f"{days} 个交易日后成交{tranche_note}" if verdict == "late"
                       else f"{signal.line_kind} 触发后 {days} 个交易日内同方向成交{tranche_note}")))
            if expected is None or filled >= expected:
                break

    sell_triggered_on = [signal.triggered_at.astimezone(SHANGHAI).date()
                         for signal in signals if signal.side == "sell"]
    first_sell_trigger = min(sell_triggered_on) if sell_triggered_on else None
    triggered_kinds = sorted({signal.line_kind for signal in signals})
    release = no_add_release(plan, evaluations)
    blocks = bool(plan.lines_of("no_add"))
    for trade in in_window:
        if trade.trade_record_id in consumed:
            continue
        blocked = blocks and trade.side == "buy" and (release is None or trade.trade_date < release)
        if blocked:
            records.append(ComplianceRecord(
                plan_id=resolved_plan_id, trade_record_id=trade.trade_record_id, line_kind="no_add",
                verdict="against_plan",
                deviation=_deviation(trade=trade, signal=None, sessions=sessions,
                                     extra={"block_released_on": release.isoformat() if release else None,
                                            "distance_to_lines": _distance_to_lines(plan, trade)}),
                notes=("禁止加仓期间买入" + (f"（{release.isoformat()} 才解除）" if release else "（计划期内未解除）"))))
        elif trade.side == "sell":
            # The note is derived from what actually triggered, never asserted.
            if first_sell_trigger is None:
                note = "计划内无任何线触发，却已卖出"
            elif trade.trade_date < first_sell_trigger:
                note = (f"最早的卖出信号在 {first_sell_trigger.isoformat()}（{'/'.join(triggered_kinds)}），"
                        f"这笔卖出早于任何触发")
            else:
                note = (f"已触发的线（{'/'.join(triggered_kinds)}）要求的数量已成交完毕，"
                        f"这笔卖出超出计划数量")
            records.append(ComplianceRecord(
                plan_id=resolved_plan_id, trade_record_id=trade.trade_record_id, line_kind=None,
                verdict="early",
                deviation=_deviation(trade=trade, signal=None, sessions=sessions,
                                     extra={"distance_to_lines": _distance_to_lines(plan, trade),
                                            "triggered_lines": triggered_kinds}),
                notes=note))
        else:
            records.append(ComplianceRecord(
                plan_id=resolved_plan_id, trade_record_id=trade.trade_record_id, line_kind=None,
                verdict="unplanned",
                deviation=_deviation(trade=trade, signal=None, sessions=sessions,
                                     extra={"distance_to_lines": _distance_to_lines(plan, trade)}),
                notes="本计划没有对应这笔成交的线"))

    return sorted(records, key=lambda record: (record.deviation.get("time_actual") or "9999",
                                               record.verdict, record.line_kind or "", record.trade_record_id or ""))


__all__ = ["BUY_ACTIONS", "FOLLOW_WINDOW_TRADING_DAYS", "RECONCILER_VERSION", "SELL_ACTIONS", "Signal",
           "TradeRecord", "VERDICTS", "no_add_release", "reconcile", "signals_from"]

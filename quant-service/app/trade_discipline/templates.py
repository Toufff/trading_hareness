"""Stage templates: structure points and time rules become evaluable lines.

Two rules shape every function here:

* moving averages describe the state, structure points drive the action.  A
  hard stop is always anchored on a real low / platform, never on an average;
* a hard stop carries exactly one evaluable condition.  Multi-condition wording
  is only allowed on the reduce/alert lines.

The structural candidate may only be *widened* (moved further from the reference
price) to reach the minimum distance the quality gate asserts; it is never
raised above the structure point, because a stop sitting inside the current
trading range is not a structure point any more.  A structure that is wider than
``3 x ATR14`` or ``12%`` is therefore left where it is and fails
``hard_stop_distance_sane``, which is the contract's own way of saying "this
structure is too far away to size".  The one exception is ``crash_rebound``,
whose primary anchor (the 20-day crash low) is a *fixed* point that a normal
rebound walks away from: when it is already beyond the sizing band the template
falls back to the nearer approved structure, the two-day low, and records which
one it used in ``structure_source``.  The widening and the fallback are part of
the recorded ``Derivation``; neither is silently applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from math import floor
from typing import Any

from ..short_term_lanes.risk import MIN_VOLATILITY_BUFFER_PCT, volatility_buffer_pct
from .contracts import Action, Confirm, Derivation, Line, Sizing, eval_expression

TEMPLATE_VERSION = "trade-discipline-templates-v3"

TARGET_EXPOSURE_PCT: dict[str, Decimal] = {
    "crash_rebound": Decimal("20"), "broken": Decimal("0"), "breakout_hold": Decimal("25"),
    "trend_hold": Decimal("30"), "pullback_hold": Decimal("30"), "base_platform": Decimal("20"),
    "unclassified": Decimal("15"),
}
# Half of the stage target over a long closure; ``broken`` is flat anyway.
# Derived, not copied, so the two tables cannot drift apart.
HOLIDAY_EXPOSURE_PCT: dict[str, Decimal] = {stage: pct / Decimal("2") for stage, pct in TARGET_EXPOSURE_PCT.items()}
DEFAULT_RISK_PER_TRADE_PCT = Decimal("1.0")
TIME_STOP_DAYS: dict[str, int] = {"crash_rebound": 3, "broken": 3}
DEFAULT_TIME_STOP_DAYS = 5
CONFIRM_REFERENCE: dict[str, str] = {
    "crash_rebound": "ma10", "broken": "ma10", "breakout_hold": "prior_high",
    "trend_hold": "ma10", "pullback_hold": "ma10", "base_platform": "prior_high",
    "unclassified": "ma10",
}
NO_ADD_REFERENCE: dict[str, str] = {"crash_rebound": "ma10", "broken": "ma5"}
TAKE_PARTIAL_STAGES = frozenset({"crash_rebound", "breakout_hold", "trend_hold"})

# The structural buffer has exactly one definition, in short_term_lanes.risk,
# so the lane report and the discipline card can never quote different numbers.
MIN_BUFFER_PCT = MIN_VOLATILITY_BUFFER_PCT
MA_STRUCTURE_BUFFER = 0.005
ATR_MIN_MULTIPLE = 0.8
ATR_MAX_MULTIPLE = 3.0
ATR_TARGET_MULTIPLE = 0.9
STOP_PCT_MIN = 0.015
STOP_PCT_MAX = 0.12
STOP_PCT_TARGET = 0.02
TRAIL_ARM_ATR_MULTIPLE = 1.0
# A soft stop needs room on both sides: at least half an ATR above the hard
# stop and half an ATR below the reference price, otherwise one day's noise
# fires it and it says nothing the hard stop does not.
SOFT_STOP_SEPARATION_ATR = 0.5
# Only a Labour Day / National Day / Spring Festival sized closure (>= 5
# calendar days) forces a holiday line; a long weekend around a one-day
# festival (3 days) does not - but it is recorded in ``omitted_lines`` so the
# refusal is auditable.  An ordinary Saturday+Sunday gap is not a closure the
# rule ever considers and leaves no record.
HOLIDAY_CLOSURE_DAYS = 5
WEEKEND_CLOSURE_DAYS = 2
LOT_SIZE = 100

# One phrase per ExtraCondition, so a label and its ``extra`` list are always
# generated from the same source.
EXTRA_CONDITION_TEXT: dict[str, str] = {
    "sector_change_negative": "行业当日翻绿", "sector_not_weak": "行业当日不走弱",
    "amount_ge_prev_day": "成交额不低于前一日", "volume_expand_1_5x": "放量至前5日均量1.5倍",
    "volume_contract_0_7x": "缩量至前5日均量0.7倍以下", "below_vwap": "最新价跌破当日VWAP",
    "after_volume_climax": "当日成交量为20日最大量且收在振幅下半",
}

PRIORITY: dict[str, int] = {
    "exposure": 0, "hard_stop": 1, "holiday": 2, "soft_stop": 3, "take_partial": 4,
    "trail": 5, "no_add": 6, "time_stop": 7, "cancel": 8, "trigger": 9,
}

# stage -> (structure sub-expression, metric keys the expression reads)
TWO_DAY_LOW_RULE: tuple[str, tuple[str, ...]] = ("min(today_low, prev_low)", ("today_low", "prev_low"))
STRUCTURE_RULE: dict[str, tuple[str, tuple[str, ...]]] = {
    "crash_rebound": ("low20", ("low20",)),
    "broken": TWO_DAY_LOW_RULE,
    "pullback_hold": ("recent_low", ("recent_low",)),
    "trend_hold": ("ma10 * (1 - ma_buffer)", ("ma10", "ma_buffer")),
    "breakout_hold": ("prior_high * (1 - ma_buffer)", ("prior_high", "ma_buffer")),
    "base_platform": ("low10_close", ("low10_close",)),
    "unclassified": TWO_DAY_LOW_RULE,
}
STAGE_STRUCTURE_NAME: dict[str, str] = {
    "crash_rebound": "急跌反弹段", "broken": "破位段", "pullback_hold": "回踩段", "trend_hold": "趋势段",
    "breakout_hold": "突破段", "base_platform": "平台段", "unclassified": "未分类（最保守）",
}
# The crash low is the only structure that a rebound leaves behind; when it is
# already beyond the sizing band the nearer approved structure takes over.
CRASH_FALLBACK_LABEL = "已超出止损距离上限，改用当日与昨日真实低点的较低者"
# The four ``min`` terms of the hard stop, in the order a tie is attributed:
# the structure point is the stop's reason for being, the other three only
# widen it.  ``derivation.inputs.binding_term`` records which one bound.
HARD_STOP_TERMS: tuple[str, ...] = ("structure", "buffer", "atr", "pct")
WIDENING_TERM_TEXT: dict[str, str] = {"atr": f"{ATR_TARGET_MULTIPLE}×ATR14", "pct": f"{STOP_PCT_TARGET:.0%}"}


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money_down(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def buffer_pct(metrics: dict[str, Any]) -> float:
    """Volatility buffer, imported from ``short_term_lanes.risk`` rather than copied."""
    return volatility_buffer_pct(metrics)


def lot_shares(equity: Decimal, exposure_pct: Decimal, price: Decimal) -> int:
    if price <= 0 or exposure_pct <= 0:
        return 0
    return int(floor(equity * exposure_pct / Decimal("100") / price / LOT_SIZE)) * LOT_SIZE


def stop_distance_terms(reference_price: Decimal, stop_price: Decimal) -> tuple[float, float]:
    """``(distance, fraction of the reference)`` exactly as ``hard_stop_distance_sane`` computes them.

    The template's fallback decision and the gate's verdict must be the same
    arithmetic, or a structure could be kept here and rejected there.
    """
    distance = float(reference_price - stop_price)
    return distance, distance / float(reference_price)


def stop_beyond_band(reference_price: Decimal, stop_price: Decimal, atr14: float) -> bool:
    """True when the stop is further away than ``3 x ATR14`` or ``12%`` - too far to size."""
    distance, fraction = stop_distance_terms(reference_price, stop_price)
    return distance > ATR_MAX_MULTIPLE * atr14 or fraction > STOP_PCT_MAX


def hard_stop_price(stage: str, metrics: dict[str, Any], reference_price: Decimal) -> tuple[Decimal, Derivation]:
    """``min(structure_low, ref x (1 - buffer), ref - 0.9 x ATR14, ref x (1 - 2%))``.

    The structure point, the volatility buffer and the two minimum-distance
    terms all sit inside one ``min``, so the result can only move *below* the
    structure point when the structure sits too close to the reference price.
    Nothing lifts it back up: a structure further away than ``3 x ATR14`` / 12%
    stays where it is and is rejected by ``hard_stop_distance_sane`` instead of
    being turned into a pure ATR number inside today's range.  Every term is a
    recorded input, so the derivation table shows which one bound.

    ``crash_rebound`` is anchored on the 20-day crash low, a fixed point the
    stage keeps for as long as the drawdown says "crash" while the price walks
    away from it.  Once that low is beyond the band the template switches to
    the two-day low - the structure ``broken``/``unclassified`` use - instead
    of rejecting a normal rebound; ``inputs.structure_source`` says which one
    was used and ``low20`` stays recorded either way.
    """
    expression, keys = STRUCTURE_RULE.get(stage, STRUCTURE_RULE["unclassified"])
    reference = float(reference_price)
    atr14 = float(metrics["atr14"])
    available = {
        "today_low": float(metrics["low"]), "prev_low": float(metrics["prev_low"]),
        "low20": float(metrics["low20"]),
        "recent_low": float(metrics["recent_low"]), "ma10": float(metrics["ma10"]),
        "prior_high": float(metrics["prior_high"]), "low10_close": float(metrics["low10_close"]),
        "ma_buffer": MA_STRUCTURE_BUFFER,
    }
    inputs: dict[str, Any] = {}
    if stage == "crash_rebound":
        crash_low = _money_down(available["low20"])
        if stop_beyond_band(reference_price, crash_low, atr14):
            expression, keys = TWO_DAY_LOW_RULE
            inputs["structure_source"] = "two_day_low"
            inputs["low20"] = available["low20"]
        else:
            inputs["structure_source"] = "low20"
    inputs.update({key: available[key] for key in keys})
    inputs.update({
        "reference_price": reference, "buffer_pct": buffer_pct(metrics), "atr14": atr14,
        "atr_target_multiple": ATR_TARGET_MULTIPLE, "stop_pct_target": STOP_PCT_TARGET,
        "band_low": max(reference - ATR_MAX_MULTIPLE * atr14, reference * (1 - STOP_PCT_MAX)),
        "band_high": min(reference - ATR_MIN_MULTIPLE * atr14, reference * (1 - STOP_PCT_MIN)),
    })
    formula = (f"min({expression}, reference_price * (1 - buffer_pct), "
               f"reference_price - atr_target_multiple * atr14, reference_price * (1 - stop_pct_target))")
    price = eval_expression(formula, inputs)
    # Which of the four terms bound.  The structure point wins a tie: the other
    # three exist only to widen it, and the label must say when they did.
    terms = {
        "structure": eval_expression(expression, inputs),
        "buffer": reference * (1 - inputs["buffer_pct"]),
        "atr": reference - ATR_TARGET_MULTIPLE * atr14,
        "pct": reference * (1 - STOP_PCT_TARGET),
    }
    inputs["structure_value"] = terms["structure"]
    inputs["binding_term"] = next(name for name in HARD_STOP_TERMS if terms[name] <= price + 1e-9)
    derivation = Derivation(rule_id=f"hard_stop.{stage}", inputs=inputs, formula=formula)
    return _money_down(price), derivation


def _price_text(value: float | Decimal) -> str:
    return format(_money(value), "f")


def structure_text(stage: str, inputs: dict[str, Any]) -> str:
    """The structure point the hard stop is anchored on, with its value, in plain Chinese."""
    if stage == "crash_rebound":
        if inputs.get("structure_source") == "two_day_low":
            two_day = min(inputs["today_low"], inputs["prev_low"])
            return f"急跌低点{_price_text(inputs['low20'])}{CRASH_FALLBACK_LABEL}{_price_text(two_day)}"
        return f"最近20个交易日最低价{_price_text(inputs['low20'])}"
    if stage == "pullback_hold":
        return f"近5日收盘低点{_price_text(inputs['recent_low'])}"
    if stage == "trend_hold":
        return f"MA10 {_price_text(inputs['ma10'])}下方半个百分点{_price_text(_money_down(inputs['structure_value']))}"
    if stage == "breakout_hold":
        return (f"突破平台{_price_text(inputs['prior_high'])}下方半个百分点"
                f"{_price_text(_money_down(inputs['structure_value']))}")
    if stage == "base_platform":
        return f"10日最低收盘{_price_text(inputs['low10_close'])}"
    two_day = min(inputs["today_low"], inputs["prev_low"])
    return f"当日与昨日真实低点的较低者{_price_text(two_day)}"


def hard_stop_note(stage: str, derivation: Derivation) -> str:
    """The parenthesis after the hard-stop sentence: which term actually bound.

    A stop that sits on the structure point says so; a stop that had to be
    widened below it names the structure it left and the term that pulled it
    down, so a reader never mistakes a ``0.9 x ATR14`` number for a low.
    """
    inputs = derivation.inputs
    stage_name = STAGE_STRUCTURE_NAME.get(stage, STAGE_STRUCTURE_NAME["unclassified"])
    structure = structure_text(stage, inputs)
    binding = inputs.get("binding_term", "structure")
    if binding == "structure":
        return f"{stage_name}，结构点：{structure}"
    widened_by = (f"波动缓冲{inputs['buffer_pct'] * 100:.2f}%" if binding == "buffer"
                  else WIDENING_TERM_TEXT[binding])
    return f"{stage_name}，结构点{structure}距离不足最小止损距离，按 {widened_by} 向下加宽"


def soft_stop_window(hard_stop: Decimal, reference_price: Decimal, atr14: float) -> tuple[Decimal, Decimal]:
    """``[hard_stop + 0.5 x ATR14, reference_price - 0.5 x ATR14]``; empty when the stop is tight."""
    gap = Decimal(str(SOFT_STOP_SEPARATION_ATR * atr14))
    return _money(hard_stop + gap), _money(reference_price - gap)


def trail_stop_price(*, anchor_price: Decimal, floor_price: Decimal,
                     previous_trail: Decimal | None = None) -> Decimal:
    """``max(prev, max(hard_stop, anchor))``: break-even once armed; a trail never moves down.

    The anchor is the average cost of a holding or the trigger price of a new
    buy, so the first trail step is "the position can no longer lose money",
    not a fraction of the arm price that could still sit under the cost.
    """
    lifted = max(_money(anchor_price), floor_price)
    if previous_trail is not None:
        lifted = max(lifted, _money(previous_trail))
    return lifted


def build_sizing(*, stage: str, equity: Decimal, risk_per_trade_pct: Decimal, reference_price: Decimal,
                 hard_stop: Decimal, current_shares: int) -> Sizing:
    """``max_shares = floor(equity x risk% / stop_distance / 100) x 100``, capped by the stage exposure."""
    target_pct = TARGET_EXPOSURE_PCT.get(stage, TARGET_EXPOSURE_PCT["unclassified"])
    stop_distance = reference_price - hard_stop
    risk_amount = (equity * risk_per_trade_pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    max_shares = int(floor(risk_amount / stop_distance / LOT_SIZE)) * LOT_SIZE
    exposure_shares = lot_shares(equity, target_pct, reference_price)
    current_exposure_pct = (Decimal(current_shares) * reference_price / equity * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    current_risk_pct = (Decimal(current_shares) * stop_distance / equity * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Sizing(
        equity=equity, risk_per_trade_pct=risk_per_trade_pct, reference_price=reference_price,
        hard_stop=hard_stop, stop_distance=stop_distance, risk_amount=risk_amount,
        max_shares=max_shares, target_exposure_pct=target_pct, current_shares=current_shares,
        current_exposure_pct=current_exposure_pct,
        recommended_shares=min(max_shares, exposure_shares),
        current_risk_pct=current_risk_pct,
    )


def closures_within(calendar: dict[str, Any], valid_until_date: str) -> list[dict[str, Any]]:
    """Every market closure inside the validity window, in calendar order.

    When ``calendar`` carries the known open ``sessions``, a gap whose
    ``last_trading_date`` is not one of them is refused: a holiday line must
    execute before a close that the exchange actually holds.
    """
    sessions = {str(value)[:10] for value in (calendar.get("sessions") or [])}
    found: list[dict[str, Any]] = []
    for gap in calendar.get("closure_gaps") or []:
        closed_days = int(gap.get("closed_days") or 0)
        last_session = str(gap.get("last_trading_date") or "")
        if sessions and last_session not in sessions:
            continue
        if closed_days >= 1 and last_session and last_session <= valid_until_date:
            found.append({"last_trading_date": last_session, "resume_date": gap.get("resume_date"),
                          "closed_days": closed_days})
    return sorted(found, key=lambda gap: gap["last_trading_date"])


def closure_within(calendar: dict[str, Any], valid_until_date: str) -> dict[str, Any] | None:
    """First market closure of >= ``HOLIDAY_CLOSURE_DAYS`` calendar days inside the validity window."""
    for gap in closures_within(calendar, valid_until_date):
        if gap["closed_days"] >= HOLIDAY_CLOSURE_DAYS:
            return gap
    return None


def is_ordinary_weekend(gap: dict[str, Any]) -> bool:
    """A Saturday+Sunday gap after a Friday session: not a closure the holiday rule considers."""
    if int(gap.get("closed_days") or 0) != WEEKEND_CLOSURE_DAYS:
        return False
    try:
        return date.fromisoformat(str(gap.get("last_trading_date"))[:10]).weekday() == 4
    except ValueError:
        return False


@dataclass
class TemplateResult:
    """The lines a template produced plus the ones it deliberately left out.

    ``omitted`` is part of the plan record (``metrics.omitted_lines``): a line
    that is absent because the rule said so must be distinguishable from a line
    that is absent because the template forgot it.
    """

    lines: list[Line] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)


def build_lines(stage: str, metrics: dict[str, Any], position: dict[str, Any] | None, sizing: Sizing,
                calendar: dict[str, Any], *, plan_kind: str = "holding", lane: dict[str, Any] | None = None,
                sector_available: bool = False, previous_trail: Decimal | None = None,
                valid_until_date: str = "") -> list[Line]:
    """The lines only; ``build_template`` also returns the omissions."""
    return build_template(stage, metrics, position, sizing, calendar, plan_kind=plan_kind, lane=lane,
                          sector_available=sector_available, previous_trail=previous_trail,
                          valid_until_date=valid_until_date).lines


def build_template(stage: str, metrics: dict[str, Any], position: dict[str, Any] | None, sizing: Sizing,
                   calendar: dict[str, Any], *, plan_kind: str = "holding", lane: dict[str, Any] | None = None,
                   sector_available: bool = False, previous_trail: Decimal | None = None,
                   valid_until_date: str = "") -> TemplateResult:
    lines: list[Line] = []
    omitted: list[dict[str, Any]] = []
    reference = sizing.reference_price
    atr14 = float(metrics["atr14"])
    equity = sizing.equity
    lane = lane or {}

    if sizing.current_exposure_pct > sizing.target_exposure_pct:
        lines.append(Line(
            kind="exposure",
            label=(f"仓位超出{stage}阶段上限{sizing.target_exposure_pct}%（当前{sizing.current_exposure_pct}%），"
                   f"下一交易日开盘15分钟内减到{sizing.recommended_shares}股"),
            metric=None, op=None, price=None, execute_by="time", execute_at="next_open+15m",
            action=Action(type="reduce_to_shares", value=sizing.recommended_shares),
            derivation=Derivation(
                rule_id=f"exposure.{stage}",
                inputs={"equity": float(equity), "reference_price": float(reference),
                        "target_exposure_pct": float(sizing.target_exposure_pct),
                        "max_shares": sizing.max_shares, "current_shares": sizing.current_shares},
                formula="min(max_shares, floor(equity * target_exposure_pct / 100 / reference_price / 100) * 100)"),
            priority=PRIORITY["exposure"]))

    hard_stop, hard_derivation = hard_stop_price(stage, metrics, reference)
    lines.append(Line(
        kind="hard_stop", label=f"日线收盘跌破{hard_stop}即全部退出（{hard_stop_note(stage, hard_derivation)}）",
        metric="daily_close", op="<", price=hard_stop, confirm=Confirm(bars=1, basis="daily"),
        extra=[], action=Action(type="exit_all"), derivation=hard_derivation,
        priority=PRIORITY["hard_stop"]))
    lines.append(Line(
        kind="hard_stop", label=f"盘中版：连续3根分钟收盘低于{hard_stop}即全部退出",
        metric="minute_close", op="<", price=hard_stop, confirm=Confirm(bars=3, basis="minute"),
        extra=[], action=Action(type="exit_all"),
        derivation=hard_derivation.model_copy(update={"rule_id": f"hard_stop.{stage}.intraday"}),
        priority=PRIORITY["hard_stop"]))

    soft_reference = _money(metrics["ma5"])
    soft_floor, soft_ceiling = soft_stop_window(hard_stop, reference, atr14)
    if soft_floor <= soft_reference <= soft_ceiling:
        soft_extra = ["sector_change_negative"] if sector_available else []
        lane_note = f"（{lane.get('lane')} 通道参考）" if lane.get("lane") else ""
        # The sentence is generated from ``soft_extra``: a condition the machine
        # does not evaluate must not appear in the wording the human reads.
        sector_note = "且行业当日翻绿" if "sector_change_negative" in soft_extra else ""
        lines.append(Line(
            kind="soft_stop",
            label=f"日线收盘跌破MA5参考{soft_reference}{sector_note}，减半仓{lane_note}",
            metric="daily_close", op="<", price=soft_reference, confirm=Confirm(bars=1, basis="daily"),
            extra=soft_extra, action=Action(type="reduce_by_pct", value=Decimal("50")),
            derivation=Derivation(rule_id=f"soft_stop.{stage}",
                                  inputs={"ma5": float(metrics["ma5"]), "hard_stop": float(hard_stop),
                                          "reference_price": float(reference), "atr14": atr14,
                                          "separation_atr": SOFT_STOP_SEPARATION_ATR},
                                  formula="ma5"),
            priority=PRIORITY["soft_stop"]))
    else:
        # Two different refusals: a window that does not exist (the stop is
        # nearer than one ATR, so no price has half an ATR on both sides) and a
        # window that exists but does not contain MA5.
        if soft_floor > soft_ceiling:
            reason = (f"软止损区间为空（下限 {soft_floor} > 上限 {soft_ceiling}，"
                      f"止损距离 {_money(reference - hard_stop)} < {2 * SOFT_STOP_SEPARATION_ATR:.1f}×ATR14 "
                      f"{_money(atr14)}），不生成")
        else:
            reason = (f"MA5 {soft_reference} 不在 [硬止损 + {SOFT_STOP_SEPARATION_ATR}×ATR14, "
                      f"参考价 − {SOFT_STOP_SEPARATION_ATR}×ATR14] = [{soft_floor}, {soft_ceiling}] 内，"
                      "软止损与现价或硬止损间距不足，一日噪音即触发，故不生成")
        omitted.append({
            "kind": "soft_stop",
            "reason": reason,
            "inputs": {"ma5": float(metrics["ma5"]), "hard_stop": float(hard_stop),
                       "reference_price": float(reference), "atr14": atr14,
                       "separation_atr": SOFT_STOP_SEPARATION_ATR,
                       "window_low": float(soft_floor), "window_high": float(soft_ceiling)},
        })

    if stage in TAKE_PARTIAL_STAGES:
        # The extra conditions are the substance of this line; the price test
        # only says "and not while it is still rising", so it is read last.
        partial_extra = ["after_volume_climax", "below_vwap"]
        partial_conditions = "且".join(EXTRA_CONDITION_TEXT[name] for name in partial_extra)
        lines.append(Line(
            kind="take_partial",
            label=f"{partial_conditions}、且最新价低于{reference}时，减半仓",
            metric="last", op="<", price=reference, confirm=Confirm(bars=1, basis="minute"),
            extra=partial_extra,
            action=Action(type="reduce_by_pct", value=Decimal("50")),
            derivation=Derivation(rule_id=f"take_partial.{stage}",
                                  inputs={"reference_price": float(reference)}, formula="reference_price"),
            priority=PRIORITY["take_partial"]))

    anchor, anchor_source = _anchor_price(position, reference, plan_kind)
    arm_price = _money(max(anchor, reference) + Decimal(str(TRAIL_ARM_ATR_MULTIPLE * atr14)))
    trail_stop = trail_stop_price(anchor_price=anchor, floor_price=hard_stop, previous_trail=previous_trail)
    trail_inputs: dict[str, Any] = {"anchor_price": float(anchor), "anchor_source": anchor_source,
                                    "floor_price": float(hard_stop)}
    if sizing.target_exposure_pct == 0:
        omitted.append({"kind": "trail",
                        "reason": f"{stage} 阶段目标仓位为 0%，仓位线已要求清仓，移动止损无意义，故不生成",
                        "inputs": {**trail_inputs, "target_exposure_pct": float(sizing.target_exposure_pct),
                                   "trail_stop": float(trail_stop)}})
    elif trail_stop <= hard_stop:
        omitted.append({"kind": "trail",
                        "reason": (f"保本目标 max(硬止损 {hard_stop}, {ANCHOR_TEXT[anchor_source]} {_money(anchor)}) "
                                   f"= {trail_stop} 不高于硬止损 {hard_stop}，“上移”不会改变止损，故不生成"),
                        "inputs": {**trail_inputs, "trail_stop": float(trail_stop),
                                   "previous_trail": float(previous_trail) if previous_trail is not None else None}})
    else:
        # ``previous_trail`` enters the formula only when there is one: the
        # formula grammar has no null, and a fabricated 0 would be a lie.
        action_formula = "max(floor_price, anchor_price)"
        target_text = f"{ANCHOR_TEXT[anchor_source]}{trail_stop}"
        if previous_trail is not None:
            trail_inputs["previous_trail"] = float(previous_trail)
            action_formula = f"max(previous_trail, {action_formula})"
            if previous_trail > max(_money(anchor), hard_stop):
                target_text = f"前序移动止损{trail_stop}（已高于{ANCHOR_TEXT[anchor_source]}{_money(anchor)}）"
        arm_text = "成本/现价孰高" if anchor_source == "average_cost" else ANCHOR_TEXT[anchor_source]
        lines.append(Line(
            kind="trail",
            label=f"日线收盘站上{arm_price}（{arm_text} + 1×ATR14）后，把止损上移到{target_text}，只上移不下移",
            metric="daily_close", op=">=", price=arm_price, confirm=Confirm(bars=1, basis="daily"),
            extra=[], action=Action(type="move_stop_to", value=trail_stop),
            derivation=Derivation(
                rule_id=f"trail.{stage}",
                inputs={"anchor_price": float(anchor), "reference_price": float(reference), "atr14": atr14,
                        "arm_multiple": TRAIL_ARM_ATR_MULTIPLE},
                formula="max(anchor_price, reference_price) + arm_multiple * atr14",
                action_inputs=trail_inputs, action_formula=action_formula),
            priority=PRIORITY["trail"]))

    if stage in NO_ADD_REFERENCE:
        key = NO_ADD_REFERENCE[stage]
        block_price = _money(metrics[key])
        lines.append(Line(
            kind="no_add", label=f"日线收盘站回{block_price}（{key.upper()}）之前禁止加仓",
            metric="daily_close", op="<", price=block_price, confirm=Confirm(bars=1, basis="daily"),
            extra=[], action=Action(type="block_add"),
            derivation=Derivation(rule_id=f"no_add.{stage}", inputs={key: float(metrics[key])}, formula=key),
            priority=PRIORITY["no_add"]))

    days = TIME_STOP_DAYS.get(stage, DEFAULT_TIME_STOP_DAYS)
    confirm_key = CONFIRM_REFERENCE.get(stage, "ma10")
    confirm_price = _money(metrics[confirm_key])
    lines.append(Line(
        kind="time_stop",
        label=f"{days}个交易日内收盘仍未站回确认线{confirm_price}（{confirm_key.upper()}）则退出",
        metric="daily_close", op="<", price=confirm_price, confirm=Confirm(bars=1, basis="daily"),
        extra=[], execute_by="time", execute_at=f"T+{days}_close", trading_days=days,
        action=Action(type="exit_all"),
        derivation=Derivation(rule_id=f"time_stop.{stage}",
                              inputs={confirm_key: float(metrics[confirm_key]), "trading_days": days},
                              formula=confirm_key),
        priority=PRIORITY["time_stop"]))

    closure = closure_within(calendar, valid_until_date)
    if closure is None:
        # A festival gap shorter than the threshold is a rule refusal, not a
        # silence: the Mid-Autumn three-day gap must be findable on the card.
        for gap in closures_within(calendar, valid_until_date):
            if is_ordinary_weekend(gap):
                continue
            omitted.append({
                "kind": "holiday",
                "reason": (f"{gap['last_trading_date']} 起休市 {gap['closed_days']} 个自然日，"
                           f"少于 {HOLIDAY_CLOSURE_DAYS} 个自然日的休市线阈值，不生成"),
                "inputs": {"closed_days": gap["closed_days"], "last_trading_date": gap["last_trading_date"],
                           "resume_date": gap.get("resume_date"), "threshold_days": HOLIDAY_CLOSURE_DAYS},
            })
    else:
        holiday_pct = HOLIDAY_EXPOSURE_PCT.get(stage, HOLIDAY_EXPOSURE_PCT["unclassified"])
        holiday_shares = min(sizing.current_shares, lot_shares(equity, holiday_pct, reference))
        lines.append(Line(
            kind="holiday",
            label=(f"{closure['last_trading_date']} 起休市{closure['closed_days']}个自然日，"
                   f"该日收盘前把仓位降到{holiday_pct}%（{holiday_shares}股）"),
            metric=None, op=None, price=None, execute_by="time",
            execute_at=f"{closure['last_trading_date']}_before_close",
            action=Action(type="reduce_to_shares", value=holiday_shares),
            derivation=Derivation(
                rule_id=f"holiday.{stage}",
                inputs={"equity": float(equity), "reference_price": float(reference),
                        "holiday_exposure_pct": float(holiday_pct), "closed_days": closure["closed_days"],
                        "current_shares": sizing.current_shares},
                formula="min(current_shares, floor(equity * holiday_exposure_pct / 100 / reference_price / 100) * 100)"),
            priority=PRIORITY["holiday"]))

    if plan_kind == "new_buy":
        lines.extend(_new_buy_lines(stage, metrics, sizing, lane, sector_available))
    return TemplateResult(lines=sorted(lines, key=lambda line: (line.priority, line.kind)), omitted=omitted)


ANCHOR_TEXT: dict[str, str] = {
    "average_cost": "成本价", "trigger_reference": "触发参考价", "reference_price": "参考价",
}


def _anchor_price(position: dict[str, Any] | None, reference: Decimal, plan_kind: str) -> tuple[Decimal, str]:
    """``(price, source)``: the average cost of a holding, the trigger price of a new buy."""
    if plan_kind == "new_buy":
        return reference, "trigger_reference"
    cost = (position or {}).get("average_cost")
    if cost is None:
        return reference, "reference_price"
    return Decimal(str(cost)), "average_cost"


def new_buy_reference(metrics: dict[str, Any], lane: dict[str, Any] | None) -> Decimal:
    """Planned entry for a ``new_buy`` plan: the lane reference, else the real platform high."""
    raw = (lane or {}).get("reference")
    return _money(raw if raw is not None else metrics["prior_high"])


def _new_buy_lines(stage: str, metrics: dict[str, Any], sizing: Sizing, lane: dict[str, Any],
                   sector_available: bool) -> list[Line]:
    reference_raw = lane.get("reference")
    support_raw = lane.get("support")
    reference_source = "lane.reference" if reference_raw is not None else "metrics.prior_high"
    support_source = "lane.support" if support_raw is not None else "metrics.recent_low"
    trigger_price = new_buy_reference(metrics, lane)
    cancel_price = _money(support_raw if support_raw is not None else metrics["recent_low"])
    trigger_extra: list[str] = ["amount_ge_prev_day"]
    if sector_available:
        trigger_extra.append("sector_not_weak")
    trigger_notes = "".join(f"且{EXTRA_CONDITION_TEXT[name]}" for name in trigger_extra)
    return [
        Line(kind="trigger",
             label=f"日线收盘站上{trigger_price}{trigger_notes}，最多买到{sizing.recommended_shares}股",
             metric="daily_close", op=">=", price=trigger_price, confirm=Confirm(bars=1, basis="daily"),
             extra=trigger_extra, action=Action(type="buy_up_to_shares", value=sizing.recommended_shares),
             derivation=Derivation(rule_id=f"trigger.{stage}",
                                   inputs={"reference": float(trigger_price), "source": reference_source},
                                   formula="reference"),
             priority=PRIORITY["trigger"]),
        Line(kind="cancel", label=f"日线收盘放量跌破{cancel_price}则本计划作废",
             metric="daily_close", op="<", price=cancel_price, confirm=Confirm(bars=1, basis="daily"),
             extra=["volume_expand_1_5x"], action=Action(type="alert"),
             derivation=Derivation(rule_id=f"cancel.{stage}",
                                   inputs={"support": float(cancel_price), "source": support_source},
                                   formula="support"),
             priority=PRIORITY["cancel"]),
    ]


__all__ = [
    "ANCHOR_TEXT", "ATR_MAX_MULTIPLE", "ATR_MIN_MULTIPLE", "ATR_TARGET_MULTIPLE", "CONFIRM_REFERENCE",
    "CRASH_FALLBACK_LABEL", "DEFAULT_RISK_PER_TRADE_PCT", "DEFAULT_TIME_STOP_DAYS", "EXTRA_CONDITION_TEXT",
    "HARD_STOP_TERMS", "HOLIDAY_CLOSURE_DAYS", "HOLIDAY_EXPOSURE_PCT", "LOT_SIZE",
    "MIN_BUFFER_PCT", "NO_ADD_REFERENCE", "PRIORITY", "SOFT_STOP_SEPARATION_ATR", "STAGE_STRUCTURE_NAME",
    "STOP_PCT_MAX", "STOP_PCT_MIN", "STOP_PCT_TARGET", "STRUCTURE_RULE", "TAKE_PARTIAL_STAGES",
    "TARGET_EXPOSURE_PCT", "TEMPLATE_VERSION", "TIME_STOP_DAYS", "TRAIL_ARM_ATR_MULTIPLE", "TWO_DAY_LOW_RULE",
    "TemplateResult", "WEEKEND_CLOSURE_DAYS", "WIDENING_TERM_TEXT",
    "buffer_pct", "build_lines", "build_sizing", "build_template", "closure_within", "closures_within",
    "hard_stop_note", "hard_stop_price", "is_ordinary_weekend", "lot_shares", "new_buy_reference",
    "soft_stop_window", "stop_beyond_band", "stop_distance_terms", "structure_text", "trail_stop_price",
]

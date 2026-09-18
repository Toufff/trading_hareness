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
structure is too far away to size".  The widening is part of the recorded
``Derivation.formula``; it is never silently applied.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from math import floor
from typing import Any

from ..short_term_lanes.risk import MIN_VOLATILITY_BUFFER_PCT, volatility_buffer_pct
from .contracts import Action, Confirm, Derivation, Line, Sizing, eval_expression

TEMPLATE_VERSION = "trade-discipline-templates-v1"

TARGET_EXPOSURE_PCT: dict[str, Decimal] = {
    "crash_rebound": Decimal("20"), "broken": Decimal("0"), "breakout_hold": Decimal("25"),
    "trend_hold": Decimal("30"), "pullback_hold": Decimal("30"), "base_platform": Decimal("20"),
    "unclassified": Decimal("15"),
}
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
TRAIL_ATR_MULTIPLE = 1.5
HOLIDAY_CLOSURE_DAYS = 3
LOT_SIZE = 100

# One phrase per ExtraCondition, so a label and its ``extra`` list are always
# generated from the same source.
EXTRA_CONDITION_TEXT: dict[str, str] = {
    "sector_change_negative": "行业当日翻绿", "sector_not_weak": "行业当日不走弱",
    "amount_ge_prev_day": "成交额不低于前一日", "volume_expand_1_5x": "放量至前5日均量1.5倍",
    "volume_contract_0_7x": "缩量至前5日均量0.7倍以下", "below_vwap": "最新价跌破当日VWAP",
    "after_volume_climax": "出现天量滞涨",
}

PRIORITY: dict[str, int] = {
    "exposure": 0, "hard_stop": 1, "holiday": 2, "soft_stop": 3, "take_partial": 4,
    "trail": 5, "no_add": 6, "time_stop": 7, "cancel": 8, "trigger": 9,
}

# stage -> (structure sub-expression, metric keys the expression reads)
STRUCTURE_RULE: dict[str, tuple[str, tuple[str, ...]]] = {
    "crash_rebound": ("min(today_low, prev_low)", ("today_low", "prev_low")),
    "broken": ("min(today_low, prev_low)", ("today_low", "prev_low")),
    "pullback_hold": ("recent_low", ("recent_low",)),
    "trend_hold": ("ma10 * (1 - ma_buffer)", ("ma10", "ma_buffer")),
    "breakout_hold": ("prior_high * (1 - ma_buffer)", ("prior_high", "ma_buffer")),
    "base_platform": ("low10_close", ("low10_close",)),
    "unclassified": ("min(today_low, prev_low)", ("today_low", "prev_low")),
}
STRUCTURE_LABEL: dict[str, str] = {
    "crash_rebound": "急跌反弹段：当日与昨日真实低点的较低者",
    "broken": "破位段：当日与昨日真实低点的较低者",
    "pullback_hold": "回踩段：近5日收盘低点",
    "trend_hold": "趋势段：MA10 下方半个百分点",
    "breakout_hold": "突破段：前5日收盘平台下方半个百分点",
    "base_platform": "平台段：10日最低收盘",
    "unclassified": "未分类：当日与昨日真实低点的较低者（最保守）",
}


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


def hard_stop_price(stage: str, metrics: dict[str, Any], reference_price: Decimal) -> tuple[Decimal, Derivation]:
    """Structure point, volatility buffer, then a downward-only widening.

    ``target_high`` is an upper bound, so the result can only move *below* the
    structure point when the structure sits too close to the reference price.
    Nothing lifts it back up: a structure further away than ``3 x ATR14`` / 12%
    stays where it is and is rejected by ``hard_stop_distance_sane`` instead of
    being turned into a pure ATR number inside today's range.
    """
    expression, keys = STRUCTURE_RULE.get(stage, STRUCTURE_RULE["unclassified"])
    reference = float(reference_price)
    atr14 = float(metrics["atr14"])
    available = {
        "today_low": float(metrics["low"]), "prev_low": float(metrics["prev_low"]),
        "recent_low": float(metrics["recent_low"]), "ma10": float(metrics["ma10"]),
        "prior_high": float(metrics["prior_high"]), "low10_close": float(metrics["low10_close"]),
        "ma_buffer": MA_STRUCTURE_BUFFER,
    }
    inputs: dict[str, Any] = {key: available[key] for key in keys}
    band_low = max(reference - ATR_MAX_MULTIPLE * atr14, reference * (1 - STOP_PCT_MAX))
    band_high = min(reference - ATR_MIN_MULTIPLE * atr14, reference * (1 - STOP_PCT_MIN))
    target_high = min(reference - ATR_TARGET_MULTIPLE * atr14, reference * (1 - STOP_PCT_TARGET))
    inputs.update({
        "reference_price": reference, "buffer_pct": buffer_pct(metrics),
        "band_low": band_low, "target_high": target_high, "atr14": atr14,
        "band_high": band_high,
    })
    formula = f"min(min({expression}, reference_price * (1 - buffer_pct)), target_high)"
    structural = min(eval_expression(expression, inputs), reference * (1 - inputs["buffer_pct"]))
    price = min(structural, target_high)
    derivation = Derivation(rule_id=f"hard_stop.{stage}", inputs=inputs, formula=formula)
    return _money_down(price), derivation


def trail_stop_price(*, arm_price: Decimal, atr14: float, low3: float, floor_price: Decimal,
                     previous_trail: Decimal | None = None) -> Decimal:
    """``max(prev, min(3-day low, arm - 1.5 x ATR))``; a trail never moves down."""
    candidate = min(Decimal(str(low3)), arm_price - Decimal(str(TRAIL_ATR_MULTIPLE * atr14)))
    lifted = max(_money(candidate), floor_price)
    if previous_trail is not None:
        lifted = max(lifted, previous_trail)
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
    return Sizing(
        equity=equity, risk_per_trade_pct=risk_per_trade_pct, reference_price=reference_price,
        hard_stop=hard_stop, stop_distance=stop_distance, risk_amount=risk_amount,
        max_shares=max_shares, target_exposure_pct=target_pct, current_shares=current_shares,
        current_exposure_pct=current_exposure_pct,
        recommended_shares=min(max_shares, exposure_shares),
    )


def closure_within(calendar: dict[str, Any], valid_until_date: str) -> dict[str, Any] | None:
    """First market closure of >= 3 calendar days starting inside the validity window.

    When ``calendar`` carries the known open ``sessions``, a gap whose
    ``last_trading_date`` is not one of them is refused: a holiday line must
    execute before a close that the exchange actually holds.
    """
    sessions = {str(value)[:10] for value in (calendar.get("sessions") or [])}
    for gap in calendar.get("closure_gaps") or []:
        closed_days = int(gap.get("closed_days") or 0)
        last_session = str(gap.get("last_trading_date") or "")
        if sessions and last_session not in sessions:
            continue
        if closed_days >= HOLIDAY_CLOSURE_DAYS and last_session and last_session <= valid_until_date:
            return {"last_trading_date": last_session, "resume_date": gap.get("resume_date"),
                    "closed_days": closed_days}
    return None


def build_lines(stage: str, metrics: dict[str, Any], position: dict[str, Any] | None, sizing: Sizing,
                calendar: dict[str, Any], *, plan_kind: str = "holding", lane: dict[str, Any] | None = None,
                sector_available: bool = False, previous_trail: Decimal | None = None,
                valid_until_date: str = "") -> list[Line]:
    lines: list[Line] = []
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
    structure_note = STRUCTURE_LABEL.get(stage, STRUCTURE_LABEL["unclassified"])
    lines.append(Line(
        kind="hard_stop", label=f"日线收盘跌破{hard_stop}即全部退出（{structure_note}）",
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
    if hard_stop < soft_reference < reference:
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
            derivation=Derivation(rule_id=f"soft_stop.{stage}", inputs={"ma5": float(metrics["ma5"])},
                                  formula="ma5"),
            priority=PRIORITY["soft_stop"]))

    if stage in TAKE_PARTIAL_STAGES:
        lines.append(Line(
            kind="take_partial",
            label=f"出现天量滞涨且最新价跌破当日VWAP时，在{reference}下方减半仓",
            metric="last", op="<", price=reference, confirm=Confirm(bars=1, basis="minute"),
            extra=["after_volume_climax", "below_vwap"],
            action=Action(type="reduce_by_pct", value=Decimal("50")),
            derivation=Derivation(rule_id=f"take_partial.{stage}",
                                  inputs={"reference_price": float(reference)}, formula="reference_price"),
            priority=PRIORITY["take_partial"]))

    anchor = _anchor_price(position, reference, plan_kind)
    arm_price = _money(max(anchor, reference) + Decimal(str(TRAIL_ARM_ATR_MULTIPLE * atr14)))
    trail_stop = trail_stop_price(arm_price=arm_price, atr14=atr14, low3=float(metrics["low3"]),
                                  floor_price=hard_stop, previous_trail=previous_trail)
    lines.append(Line(
        kind="trail", label=f"最新价站上{arm_price}（成本/现价孰高 + 1×ATR）后，把止损上移到{trail_stop}，只上移不下移",
        metric="last", op=">=", price=arm_price, confirm=Confirm(bars=1, basis="daily"),
        extra=[], action=Action(type="move_stop_to", value=trail_stop),
        derivation=Derivation(
            rule_id=f"trail.{stage}",
            inputs={"anchor_price": float(anchor), "reference_price": float(reference), "atr14": atr14,
                    "arm_multiple": TRAIL_ARM_ATR_MULTIPLE, "trail_multiple": TRAIL_ATR_MULTIPLE,
                    "low3": float(metrics["low3"]), "floor_price": float(hard_stop),
                    "trail_stop": float(trail_stop),
                    "previous_trail": float(previous_trail) if previous_trail is not None else None},
            formula="max(anchor_price, reference_price) + arm_multiple * atr14"),
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
    if closure:
        holiday_pct = Decimal("0") if stage in {"crash_rebound", "broken"} else sizing.target_exposure_pct / 2
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
    return sorted(lines, key=lambda line: (line.priority, line.kind))


def _anchor_price(position: dict[str, Any] | None, reference: Decimal, plan_kind: str) -> Decimal:
    if plan_kind == "new_buy":
        return reference
    cost = (position or {}).get("average_cost")
    if cost is None:
        return reference
    return Decimal(str(cost))


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
    "ATR_MAX_MULTIPLE", "ATR_MIN_MULTIPLE", "ATR_TARGET_MULTIPLE", "CONFIRM_REFERENCE",
    "DEFAULT_RISK_PER_TRADE_PCT", "DEFAULT_TIME_STOP_DAYS", "EXTRA_CONDITION_TEXT",
    "HOLIDAY_CLOSURE_DAYS", "LOT_SIZE",
    "MIN_BUFFER_PCT", "NO_ADD_REFERENCE", "PRIORITY", "STOP_PCT_MAX", "STOP_PCT_MIN", "STOP_PCT_TARGET",
    "STRUCTURE_RULE", "TAKE_PARTIAL_STAGES", "TARGET_EXPOSURE_PCT", "TEMPLATE_VERSION", "TIME_STOP_DAYS",
    "TRAIL_ARM_ATR_MULTIPLE", "TRAIL_ATR_MULTIPLE", "buffer_pct", "build_lines", "build_sizing",
    "closure_within", "hard_stop_price", "lot_shares", "new_buy_reference", "trail_stop_price",
]

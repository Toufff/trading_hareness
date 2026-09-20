"""Plan quality gate: pure assertions over a finished ``DisciplinePlan``.

A failing gate never discards a plan.  ``generator`` persists it with
``status="rejected_by_quality"`` and the failing checks attached, so a bad
template shows up as a durable record instead of a silently softened text plan.

Every check reads only the plan and its frozen ``metrics`` snapshot, so the same
verdict can be recomputed from a stored row months later.
"""

from __future__ import annotations

from decimal import Decimal
from math import floor
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import SECTOR_CONDITIONS, DisciplinePlan, FormulaError, Line, QualityCheck
from .templates import (
    ATR_MAX_MULTIPLE,
    ATR_MIN_MULTIPLE,
    HOLIDAY_CLOSURE_DAYS,
    LOT_SIZE,
    STOP_PCT_MAX,
    STOP_PCT_MIN,
    closure_within,
    lot_shares,
    soft_stop_window,
    stop_distance_terms,
)

DERIVATION_TOLERANCE = 0.01 + 1e-9
_SHANGHAI = ZoneInfo("Asia/Shanghai")
CHECK_IDS = (
    "has_hard_stop", "has_time_stop", "hard_stop_below_price", "hard_stop_single_condition",
    "hard_stop_distance_sane", "soft_above_hard", "soft_stop_separation", "lines_monotonic",
    "every_line_evaluable", "every_line_has_derivation", "exposure_line_when_over_cap",
    "holiday_line_when_closure", "no_add_when_crash_or_broken", "sizing_consistent",
    "not_lowered_vs_previous", "valid_until_within_5_trading_days", "entry_reference_current",
    "buy_zone_valid",
)
STALE_DAY_PREFIX = "bars_stale_day:"
ENTRY_TOLERANCE = 0.01 + 1e-9


def _check(check_id: str, passed: bool, detail: str) -> QualityCheck:
    return QualityCheck(check_id=check_id, passed=passed, detail=detail[:400])


def _reference_price(plan: DisciplinePlan) -> Decimal | None:
    if plan.sizing is not None:
        return plan.sizing.reference_price
    raw = plan.metrics.get("reference_price")
    return Decimal(str(raw)) if raw is not None else None


def _daily_hard_stop(plan: DisciplinePlan) -> Decimal | None:
    prices = [line.price for line in plan.lines_of("hard_stop")
              if line.price is not None and line.confirm.basis == "daily"]
    return min(prices) if prices else None


def _line_evaluable(line: Line, sector_available: bool) -> str:
    if any(condition in SECTOR_CONDITIONS for condition in line.extra) and not sector_available:
        return f"{line.kind} 使用了行业条件但没有已存储的板块归属"
    if line.execute_by == "time":
        return "" if line.execute_at else f"{line.kind} 按时间执行却没有 execute_at"
    if line.metric and line.op and (line.price is not None or line.pct is not None):
        return ""
    return f"{line.kind} 缺少可评估的 metric/op/price 组合"


def _derivation_problem(line: Line) -> str:
    """Recompute the price from ``formula`` and, where the action carries a
    derived number, the action value from ``action_formula``.

    A price-less time line (exposure / holiday) records the share count in its
    ``formula``; that count is recomputed against ``action.value`` so the number
    the human is told to trade is the number the inputs produce.
    """
    derivation = line.derivation
    if not derivation.inputs:
        return f"{line.kind} 的 derivation.inputs 为空"
    if not derivation.formula.strip():
        return f"{line.kind} 的 derivation.formula 为空"
    try:
        recomputed = derivation.recompute()
    except FormulaError as error:
        return f"{line.kind} 的 formula 无法复算：{error}"
    if line.price is not None:
        if abs(recomputed - float(line.price)) > DERIVATION_TOLERANCE:
            return f"{line.kind} 复算得到 {recomputed:.4f}，与记录的 {line.price} 不符"
    elif line.action.value is not None and abs(recomputed - float(line.action.value)) > DERIVATION_TOLERANCE:
        return f"{line.kind} 复算股数 {recomputed:.0f}，与动作值 {line.action.value} 不符"
    if line.action.type == "move_stop_to":
        if not derivation.action_formula.strip() or not derivation.action_inputs:
            return f"{line.kind} 的 move_stop_to 目标缺少 action_formula/action_inputs"
        if line.action.value is None:
            return f"{line.kind} 的 move_stop_to 没有目标价"
        try:
            target = derivation.recompute_action()
        except FormulaError as error:
            return f"{line.kind} 的 action_formula 无法复算：{error}"
        if abs(target - float(line.action.value)) > DERIVATION_TOLERANCE:
            return f"{line.kind} 动作值复算得到 {target:.4f}，与记录的 {line.action.value} 不符"
    return ""


def _required_closure(plan: DisciplinePlan) -> dict[str, Any] | None:
    """The closure that demands a holiday line, re-derived from the frozen calendar.

    The gate never trusts ``metrics.closure_required``: it runs the template's
    own ``closure_within`` over ``metrics.calendar`` (the gaps and the open
    sessions the generator saw) against the plan's ``valid_until``.  A plan
    stored before the calendar was frozen falls back to the recorded closure,
    still re-checked against the threshold.
    """
    metrics: dict[str, Any] = plan.metrics or {}
    calendar = metrics.get("calendar")
    if isinstance(calendar, dict):
        return closure_within(calendar, plan.valid_until.astimezone(_SHANGHAI).date().isoformat())
    closure = metrics.get("closure") if isinstance(metrics.get("closure"), dict) else None
    if closure is not None and int(closure.get("closed_days") or 0) >= HOLIDAY_CLOSURE_DAYS:
        return closure
    return None


def _entry_problem(plan: DisciplinePlan) -> str:
    """Why a new buy's entry price is not a current, recomputable close (``""`` when it is).

    ``entry_price`` must be ``max(lane_reference, last_close)`` where
    ``last_close`` is the plan trading date's own close (settled, or a later
    forming bar), and the sizing must be taken on exactly that price.  A plan
    that sized off the lane's structural reference - the pre-v3 behaviour that
    put 13.9% between 000811.SZ's real entry and its stop - carries no
    ``metrics.entry`` and fails here.
    """
    entry = (plan.metrics or {}).get("entry")
    if not isinstance(entry, dict):
        return "缺少 metrics.entry：入场参考价不是按 max(lane 结构参考价, 最新已结算收盘) 推导的"
    try:
        lane_reference = float(entry["lane_reference"])
        last_close = float(entry["last_close"])
        entry_price = float(entry["entry_price"])
    except (KeyError, TypeError, ValueError):
        return "metrics.entry 缺少 lane_reference / last_close / entry_price"
    close_day = str(entry.get("last_close_date") or "")[:10]
    if close_day < plan.trading_date.isoformat():
        return f"入场参考收盘日 {close_day or '缺失'} 早于计划交易日 {plan.trading_date.isoformat()}"
    stale = [ref for ref in plan.evidence_refs if ref.startswith(STALE_DAY_PREFIX)]
    if stale:
        return f"当日日线尚未结算，入场参考价取自更早的收盘（{stale[0]}）"
    expected = max(lane_reference, last_close)
    if abs(entry_price - expected) > ENTRY_TOLERANCE:
        return f"entry_price {entry_price} 与 max(lane 参考 {lane_reference}, 收盘 {last_close}) = {expected} 不符"
    if plan.sizing is None or abs(float(plan.sizing.reference_price) - entry_price) > ENTRY_TOLERANCE:
        sized = None if plan.sizing is None else plan.sizing.reference_price
        return f"仓位按 {sized} 计算，而不是入场参考价 {entry_price}"
    return ""


def evaluate_quality(plan: DisciplinePlan) -> list[QualityCheck]:
    """Return every assertion's verdict, in a stable order."""
    metrics: dict[str, Any] = plan.metrics or {}
    sizing = plan.sizing
    reference = _reference_price(plan)
    hard_stops = plan.lines_of("hard_stop")
    hard_stop = _daily_hard_stop(plan)
    sector_available = bool(metrics.get("sector_available"))
    checks: list[QualityCheck] = []

    checks.append(_check("has_hard_stop", bool(hard_stop is not None),
                         f"hard_stop 线 {len(hard_stops)} 条" if hard_stops else "缺少 hard_stop 线"))
    time_stops = plan.lines_of("time_stop")
    checks.append(_check("has_time_stop", bool(time_stops),
                         f"time_stop {time_stops[0].execute_at}" if time_stops else "缺少 time_stop 线"))

    if hard_stop is None or reference is None:
        checks.append(_check("hard_stop_below_price", False, "无法比较：缺少 hard_stop 或参考价"))
    else:
        checks.append(_check("hard_stop_below_price", hard_stop < reference,
                             f"hard_stop {hard_stop} vs 参考价 {reference}"))

    multi = [line.kind for line in hard_stops if line.extra]
    checks.append(_check("hard_stop_single_condition", not multi,
                         f"hard_stop 附带了额外条件 {multi}" if multi else "hard_stop 只含单一可评估条件"))

    atr14 = metrics.get("atr14")
    if hard_stop is None or reference is None or not atr14:
        checks.append(_check("hard_stop_distance_sane", False, "无法计算止损距离：缺少 ATR14 或参考价"))
    else:
        distance, pct = stop_distance_terms(reference, hard_stop)
        atr_ok = ATR_MIN_MULTIPLE * float(atr14) <= distance <= ATR_MAX_MULTIPLE * float(atr14)
        pct_ok = STOP_PCT_MIN <= pct <= STOP_PCT_MAX
        checks.append(_check(
            "hard_stop_distance_sane", atr_ok and pct_ok,
            f"距离 {distance:.3f}（{pct * 100:.2f}%），ATR14 {float(atr14):.3f} 允许区间 "
            f"[{ATR_MIN_MULTIPLE * float(atr14):.3f}, {ATR_MAX_MULTIPLE * float(atr14):.3f}]"))

    soft_stops = [line for line in plan.lines_of("soft_stop") if line.price is not None]
    if not soft_stops:
        checks.append(_check("soft_above_hard", True, "未使用软止损线"))
    elif hard_stop is None:
        checks.append(_check("soft_above_hard", False, "有软止损但没有硬止损"))
    else:
        bad = [str(line.price) for line in soft_stops if line.price is not None and line.price <= hard_stop]
        checks.append(_check("soft_above_hard", not bad,
                             f"软止损 {bad} 不高于硬止损 {hard_stop}" if bad else "软止损均高于硬止损"))

    if not soft_stops:
        checks.append(_check("soft_stop_separation", True, "未使用软止损线"))
    elif hard_stop is None or reference is None or not atr14:
        checks.append(_check("soft_stop_separation", False, "无法校验软止损间距：缺少 hard_stop、参考价或 ATR14"))
    else:
        window_low, window_high = soft_stop_window(hard_stop, reference, float(atr14))
        crowded = [str(line.price) for line in soft_stops
                   if line.price is not None and not window_low <= line.price <= window_high]
        checks.append(_check(
            "soft_stop_separation", not crowded,
            f"软止损 {crowded} 不在 [{window_low}, {window_high}]（硬止损+0.5×ATR14, 参考价−0.5×ATR14）内"
            if crowded else f"软止损与硬止损、参考价各相距至少 0.5×ATR14（[{window_low}, {window_high}]）"))

    monotonic_problems: list[str] = []
    if hard_stop is not None and reference is not None:
        if hard_stop >= reference:
            monotonic_problems.append(f"hard_stop {hard_stop} 不低于参考价 {reference}")
        for line in soft_stops:
            if line.price is not None and not hard_stop < line.price < reference:
                monotonic_problems.append(f"soft_stop {line.price} 不在 ({hard_stop}, {reference}) 之间")
        for line in plan.lines_of("trail"):
            if line.price is not None and line.price <= reference:
                monotonic_problems.append(f"trail 触发价 {line.price} 未高于参考价 {reference}")
        for line in plan.lines_of("trigger"):
            if line.price is not None and line.price <= hard_stop:
                monotonic_problems.append(f"买入触发 {line.price} 不高于硬止损 {hard_stop}")
    else:
        monotonic_problems.append("缺少 hard_stop 或参考价")
    checks.append(_check("lines_monotonic", not monotonic_problems,
                         "；".join(monotonic_problems) if monotonic_problems else "hard < soft < 参考价，trail 触发价在上方"))

    evaluable = [problem for problem in (_line_evaluable(line, sector_available) for line in plan.lines) if problem]
    checks.append(_check("every_line_evaluable", not evaluable,
                         "；".join(evaluable) if evaluable else f"{len(plan.lines)} 条线全部可评估"))

    derivations = [problem for problem in (_derivation_problem(line) for line in plan.lines) if problem]
    checks.append(_check("every_line_has_derivation", not derivations,
                         "；".join(derivations) if derivations else f"{len(plan.lines)} 条线均可从 inputs 复算"))

    exposure_lines = plan.lines_of("exposure")
    if sizing is None:
        checks.append(_check("exposure_line_when_over_cap", not exposure_lines, "无 sizing，不应出现仓位线"))
    elif sizing.current_shares > sizing.recommended_shares:
        matched = [line for line in exposure_lines
                   if line.action.type == "reduce_to_shares" and line.action.value == sizing.recommended_shares]
        checks.append(_check("exposure_line_when_over_cap", bool(matched),
                             f"当前 {sizing.current_shares} 股超过建议 {sizing.recommended_shares} 股（风险上限 "
                             f"{sizing.max_shares} / 阶段上限 {sizing.cap_shares} 股），"
                             f"{'已给出减仓线' if matched else '缺少减到 ' + str(sizing.recommended_shares) + ' 股的仓位线'}"))
    else:
        checks.append(_check("exposure_line_when_over_cap", not exposure_lines,
                             f"当前 {sizing.current_shares} 股未超过建议 {sizing.recommended_shares} 股"
                             + ("，却给出了减仓线" if exposure_lines else "")))

    closure = _required_closure(plan)
    holiday_basis = ((metrics.get("exposure_calibration") or {}).get("holiday")
                     if isinstance(metrics.get("exposure_calibration"), dict) else None)
    holiday_not_tighter = (isinstance(holiday_basis, dict) and sizing is not None
                           and Decimal(str(holiday_basis.get("cap_pct"))) >= sizing.target_exposure_pct)
    if closure is not None and holiday_not_tighter:
        # The calibrated holiday cap is not tighter than the stage cap: no line is required
        # (the template records the refusal in omitted_lines).
        closure = None
    closure_required = closure is not None
    holiday_lines = plan.lines_of("holiday")
    closure_note = (f"{closure['last_trading_date']} 起休市 {closure['closed_days']} 个自然日"
                    if closure else "")
    checks.append(_check("holiday_line_when_closure", (not closure_required) or bool(holiday_lines),
                         f"有效期内有 ≥{HOLIDAY_CLOSURE_DAYS} 个自然日的休市（{closure_note}）且已给出休市线"
                         if closure_required and holiday_lines
                         else (f"有效期内有 ≥{HOLIDAY_CLOSURE_DAYS} 个自然日的休市（{closure_note}）但缺少休市线"
                               if closure_required else f"有效期内无 ≥{HOLIDAY_CLOSURE_DAYS} 个自然日的休市")))

    needs_no_add = plan.stage in {"crash_rebound", "broken"}
    no_add_lines = plan.lines_of("no_add")
    checks.append(_check("no_add_when_crash_or_broken", (not needs_no_add) or bool(no_add_lines),
                         f"{plan.stage} 需要禁加仓线" + ("，已给出" if no_add_lines else "，但缺失")
                         if needs_no_add else f"{plan.stage} 不强制禁加仓线"))

    if sizing is None:
        checks.append(_check("sizing_consistent", False, "缺少 sizing"))
    else:
        expected_risk = (sizing.equity * sizing.risk_per_trade_pct / Decimal("100"))
        expected_distance = sizing.reference_price - sizing.hard_stop
        # Both limits are judged at the worst price the plan permits, not at the
        # reference. Recomputing at the reference is precisely why this check
        # passed on cards whose own buy zone allowed 1.5% against a 1% budget.
        sizing_price = sizing.sizing_price if sizing.sizing_price is not None else sizing.reference_price
        sizing_distance = (sizing.sizing_distance if sizing.sizing_distance is not None
                           else sizing_price - sizing.hard_stop)
        expected_max = int(floor(sizing.risk_amount / sizing_distance / LOT_SIZE)) * LOT_SIZE
        cap_shares = lot_shares(sizing.equity, sizing.target_exposure_pct, sizing_price)
        problems: list[str] = []
        if sizing_price < sizing.reference_price:
            problems.append(f"定量价 {sizing_price} 低于参考价 {sizing.reference_price}")
        if sizing_distance != sizing_price - sizing.hard_stop:
            problems.append(f"定量距离 {sizing_distance} 与 定量价−止损 {sizing_price - sizing.hard_stop} 不符")
        # The property the formulas exist to deliver, asserted directly: a fill
        # anywhere the card allows must stay inside the tolerance.
        worst_risk_pct = (Decimal(sizing.recommended_shares) * sizing_distance / sizing.equity * Decimal("100"))
        if worst_risk_pct > sizing.risk_per_trade_pct + Decimal("0.0001"):
            problems.append(f"区间上沿 {sizing_price} 处风险 {worst_risk_pct:.3f}% 超过容忍度 "
                            f"{sizing.risk_per_trade_pct}%")
        if abs(expected_risk - sizing.risk_amount) > Decimal("0.01"):
            problems.append(f"风险预算 {sizing.risk_amount} 与 equity×risk% {expected_risk} 不符")
        if expected_distance != sizing.stop_distance:
            problems.append(f"止损距离 {sizing.stop_distance} 与 参考价−止损 {expected_distance} 不符")
        if expected_max != sizing.max_shares:
            problems.append(f"max_shares {sizing.max_shares} 与公式复算 {expected_max} 不符")
        if sizing.recommended_shares > sizing.max_shares:
            problems.append(f"recommended_shares {sizing.recommended_shares} 超过 max_shares {sizing.max_shares}")
        if sizing.recommended_shares > cap_shares:
            problems.append(f"recommended_shares {sizing.recommended_shares} 超过仓位上限 {cap_shares} 股")
        if sizing.cap_shares is not None and sizing.recommended_shares != min(expected_max, cap_shares):
            problems.append(f"recommended_shares {sizing.recommended_shares} 不等于 min(风险上限 {expected_max}, "
                            f"阶段上限 {cap_shares})")
        checks.append(_check("sizing_consistent", not problems,
                             "；".join(problems) if problems else
                             f"max_shares {sizing.max_shares}，建议 {sizing.recommended_shares} 股"))

    previous = metrics.get("previous_hard_stop")
    if previous is None or hard_stop is None:
        checks.append(_check("not_lowered_vs_previous", True, "无前序 active 计划可比较"))
    else:
        lowered = float(hard_stop) < float(previous) - 1e-9
        checks.append(_check("not_lowered_vs_previous", (not lowered) or bool(plan.lowered_reason),
                             f"硬止损 {hard_stop} 低于前序 {previous}，"
                             f"{'已记录下调理由' if plan.lowered_reason else '且没有 lowered_reason'}"
                             if lowered else f"硬止损 {hard_stop} 不低于前序 {previous}"))

    limit = str(metrics.get("valid_until_limit") or "")[:10]
    if not limit:
        checks.append(_check("valid_until_within_5_trading_days", False, "缺少交易日历，无法校验有效期"))
    else:
        checks.append(_check("valid_until_within_5_trading_days", plan.valid_until.date().isoformat() <= limit,
                             f"valid_until {plan.valid_until.date().isoformat()}，第5个交易日 {limit}"))

    if plan.plan_kind != "new_buy":
        checks.append(_check("buy_zone_valid", True, "持仓计划：没有买入区间，不适用"))
    else:
        trigger = next((line.price for line in plan.lines_of("trigger") if line.price is not None), None)
        cap = next((line.price for line in plan.lines_of("chase_cap") if line.price is not None), None)
        if trigger is None or cap is None or hard_stop is None:
            checks.append(_check("buy_zone_valid", False, "新买计划缺少买入触发、追高上限或硬止损，无法确定买入区间"))
        else:
            valid = hard_stop < trigger <= cap
            checks.append(_check(
                "buy_zone_valid", valid,
                f"买入区间 [{trigger}, {cap}]，硬止损 {hard_stop}" if valid else
                (f"买入区间为空：触发下沿 {trigger} 高于追高上限 {cap}（硬止损 {hard_stop} + 0.5×ATR14 已越过追高上限），不可买"
                 if trigger > cap else f"买入触发 {trigger} 不高于硬止损 {hard_stop}，满足买入的收盘同时满足退出")))

    if plan.plan_kind != "new_buy":
        checks.append(_check("entry_reference_current", True, "持仓计划：参考价即最新收盘，不适用"))
    else:
        problem = _entry_problem(plan)
        entry = (metrics.get("entry") or {}) if not problem else {}
        checks.append(_check(
            "entry_reference_current", not problem,
            problem or (f"入场参考价 {entry.get('entry_price')} = max(lane 参考 {entry.get('lane_reference')}, "
                        f"{entry.get('last_close_date')} 收盘 {entry.get('last_close')})，仓位与止损均按它计算")))
    return checks


def quality_passed(checks: list[QualityCheck]) -> bool:
    return all(check.passed for check in checks)


def failed_checks(checks: list[QualityCheck]) -> list[str]:
    return [check.check_id for check in checks if not check.passed]


__all__ = ["CHECK_IDS", "DERIVATION_TOLERANCE", "ENTRY_TOLERANCE", "evaluate_quality", "failed_checks", "quality_passed"]

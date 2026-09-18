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

from .contracts import SECTOR_CONDITIONS, DisciplinePlan, FormulaError, Line, QualityCheck
from .templates import (
    ATR_MAX_MULTIPLE,
    ATR_MIN_MULTIPLE,
    LOT_SIZE,
    STOP_PCT_MAX,
    STOP_PCT_MIN,
    lot_shares,
)

DERIVATION_TOLERANCE = 0.01 + 1e-9
CHECK_IDS = (
    "has_hard_stop", "has_time_stop", "hard_stop_below_price", "hard_stop_single_condition",
    "hard_stop_distance_sane", "soft_above_hard", "lines_monotonic", "every_line_evaluable",
    "every_line_has_derivation", "exposure_line_when_over_cap", "holiday_line_when_closure",
    "no_add_when_crash_or_broken", "sizing_consistent", "not_lowered_vs_previous",
    "valid_until_within_5_trading_days",
)


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
    derivation = line.derivation
    if not derivation.inputs:
        return f"{line.kind} 的 derivation.inputs 为空"
    if not derivation.formula.strip():
        return f"{line.kind} 的 derivation.formula 为空"
    if line.price is None:
        return ""
    try:
        recomputed = derivation.recompute()
    except FormulaError as error:
        return f"{line.kind} 的 formula 无法复算：{error}"
    if abs(recomputed - float(line.price)) > DERIVATION_TOLERANCE:
        return f"{line.kind} 复算得到 {recomputed:.4f}，与记录的 {line.price} 不符"
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
        distance = float(reference - hard_stop)
        pct = distance / float(reference)
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
    elif sizing.current_exposure_pct > sizing.target_exposure_pct:
        matched = [line for line in exposure_lines
                   if line.action.type == "reduce_to_shares" and line.action.value == sizing.recommended_shares]
        checks.append(_check("exposure_line_when_over_cap", bool(matched),
                             f"当前仓位 {sizing.current_exposure_pct}% 超过上限 {sizing.target_exposure_pct}%，"
                             f"{'已给出减仓线' if matched else '缺少减到 ' + str(sizing.recommended_shares) + ' 股的仓位线'}"))
    else:
        checks.append(_check("exposure_line_when_over_cap", True,
                             f"当前仓位 {sizing.current_exposure_pct}% 未超过上限 {sizing.target_exposure_pct}%"))

    closure_required = bool(metrics.get("closure_required"))
    holiday_lines = plan.lines_of("holiday")
    checks.append(_check("holiday_line_when_closure", (not closure_required) or bool(holiday_lines),
                         "有效期内有长假且已给出休市线" if closure_required and holiday_lines
                         else ("有效期内有长假但缺少休市线" if closure_required else "有效期内无长假")))

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
        expected_max = int(floor(sizing.risk_amount / sizing.stop_distance / LOT_SIZE)) * LOT_SIZE
        cap_shares = lot_shares(sizing.equity, sizing.target_exposure_pct, sizing.reference_price)
        problems: list[str] = []
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
    return checks


def quality_passed(checks: list[QualityCheck]) -> bool:
    return all(check.passed for check in checks)


def failed_checks(checks: list[QualityCheck]) -> list[str]:
    return [check.check_id for check in checks if not check.passed]


__all__ = ["CHECK_IDS", "DERIVATION_TOLERANCE", "evaluate_quality", "failed_checks", "quality_passed"]

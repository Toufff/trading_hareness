"""The discipline card: one plan rendered as Markdown and as JSON.

Rendering is the last pure step of the pipeline.  It invents nothing: every
number printed here already exists on the ``DisciplinePlan``, and the derivation
table prints the ``rule_id``/``inputs``/``formula`` that produced each price
together with the value recomputed from those very inputs, so a reviewer can
check a line without opening the database.

Two properties matter and are asserted by the tests:

* **complete** - every line and every quality check appears in the card; a
  failing gate is printed, never hidden;
* **deterministic** - the same plan always renders byte-identical output.  There
  is no clock, no locale and no dictionary-order dependence in here.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import ComplianceRecord, DisciplinePlan, Evaluation, FormulaError, Line
from .generator import t1_locked_shares_for
from .quality import CHECK_IDS, DERIVATION_TOLERANCE

REPORT_VERSION = "trade-discipline-report-v7"
RESEARCH_NOTICE = "研究用途，仅作人工决策依据：系统不连券商、不下单、不改持仓。"
DASH = "—"

STAGE_LABEL: dict[str, str] = {
    "crash_rebound": "急跌反弹段", "broken": "破位段", "breakout_hold": "突破持有段",
    "trend_hold": "趋势持有段", "pullback_hold": "回踩持有段", "base_platform": "平台整理段",
    "unclassified": "未分类（最保守模板）",
}
PLAN_KIND_LABEL: dict[str, str] = {"holding": "持仓计划", "new_buy": "新买入计划"}
STATUS_LABEL: dict[str, str] = {
    "active": "生效中", "rejected_by_quality": "未通过质量门（仍已落库）",
    "superseded": "已被新计划替代", "expired": "已过期",
}
KIND_LABEL: dict[str, str] = {
    "exposure": "仓位", "hard_stop": "硬止损", "soft_stop": "软止损", "trail": "移动止损",
    "time_stop": "时间止损", "no_add": "禁加仓", "take_partial": "减半仓", "holiday": "休市减仓",
    "trigger": "买入触发", "cancel": "计划作废", "chase_cap": "追高上限",
}
METRIC_LABEL: dict[str, str] = {
    "daily_close": "日线收盘价", "minute_close": "分钟收盘价", "last": "最新价",
    "low": "当日最低价", "high": "当日最高价", "vwap": "当日均价VWAP",
}
OP_LABEL: dict[str, str] = {"<": "低于", "<=": "不高于", ">": "高于", ">=": "不低于"}
EXTRA_LABEL: dict[str, str] = {
    "sector_change_negative": "所属行业当日翻绿", "sector_not_weak": "所属行业当日不弱（涨幅≥0）",
    "amount_ge_prev_day": "成交额不低于前一日", "volume_expand_1_5x": "成交量≥前5日均量1.5倍",
    "volume_contract_0_7x": "成交量≤前5日均量0.7倍", "below_vwap": "最新价跌破当日VWAP",
    "after_volume_climax": "当日成交量为20日最大量且收在振幅下半（天量滞涨）",
}
ACTION_LABEL: dict[str, str] = {
    "exit_all": "全部退出", "reduce_to_shares": "减到 {value} 股", "reduce_by_pct": "减仓 {value}%",
    "move_stop_to": "把止损上移到 {value}", "block_add": "禁止加仓", "alert": "提醒复核",
    "buy_up_to_shares": "最多买到 {value} 股",
}
EXECUTE_AT_LABEL: dict[str, str] = {"next_open+15m": "下一交易日开盘后15分钟内"}
LINE_STATE_LABEL: dict[str, str] = {
    "armed": "待触发", "triggered": "已触发", "expired": "已过期", "cancelled": "已取消",
    "capped": "已越过追高上限，不买",
}
PLAN_STATE_LABEL: dict[str, str] = {
    "active": "仍在计划内", "exit_signalled": "已发出退出信号", "reduce_signalled": "已发出减仓信号",
    "expired": "已过期/作废",
}
VERDICT_LABEL: dict[str, str] = {
    "followed": "遵守", "early": "提前动手", "late": "迟于纪律", "missed": "该做未做",
    "against_plan": "违反纪律", "unplanned": "计划外",
}
CHECK_LABEL: dict[str, str] = {
    "has_hard_stop": "必须有硬止损线",
    "has_time_stop": "必须有时间止损线",
    "hard_stop_below_price": "硬止损低于参考价",
    "hard_stop_single_condition": "硬止损只带单一条件",
    "hard_stop_distance_sane": "止损距离落在 ATR 与百分比的合理区间",
    "soft_above_hard": "软止损高于硬止损",
    "soft_stop_separation": "软止损与硬止损、参考价各相距至少 0.5×ATR14",
    "lines_monotonic": "硬止损 < 软止损 < 参考价，移动止损触发价在上方",
    "every_line_evaluable": "每条线都可被系统评估",
    "every_line_has_derivation": "每条线都能从 inputs 复算出价格与动作值",
    "exposure_line_when_over_cap": "旧版：仓位超阶段上限时必须给出减仓线",
    "exposure_line_when_over_risk": "止损风险超预算时必须给出减仓线",
    "holiday_line_when_closure": "有效期内有 ≥5 个自然日休市时必须给出休市线",
    "no_add_when_crash_or_broken": "急跌/破位阶段必须禁加仓",
    "sizing_consistent": "止损风险仓位公式可复算；集中度压力值仅提示",
    "not_lowered_vs_previous": "硬止损不得低于前序计划（除非记录下调理由）",
    "valid_until_within_5_trading_days": "有效期不超过5个交易日",
    "entry_reference_current": "新买入场参考价取计划交易日收盘或更晚，仓位与止损按它计算",
    "buy_zone_valid": "新买买入区间有效：硬止损 < 触发下沿 ≤ 追高上限",
}


# --------------------------------------------------------------------------
# Formatting helpers (pure, locale-free)
# --------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    """One deterministic rendering for every number the card prints."""
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (Decimal, int, float)):
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except (ArithmeticError, ValueError):
            return str(value)
    return str(value)


def _cell(value: Any) -> str:
    """Escape one Markdown table cell; a table never breaks on a label."""
    text = _fmt(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)
    return lines


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _pct(value: Any) -> str:
    return DASH if value is None else f"{_fmt(value)}%"


# --------------------------------------------------------------------------
# Line wording
# --------------------------------------------------------------------------
def condition_text(line: Line) -> str:
    """The line's trigger condition in plain Chinese, never a template dump.

    The ``extra`` conditions are read first: on a multi-condition line they are
    the substance (a volume climax, a sector turning red) and the price test is
    the weakest clause, so it closes the sentence instead of opening it.
    """
    if line.execute_by == "time":
        when = EXECUTE_AT_LABEL.get(line.execute_at or "", line.execute_at or DASH)
        if line.kind == "time_stop" and line.trading_days:
            reclaim = f"收盘仍未站回 {_fmt(line.price)}" if line.price is not None else "仍未确认"
            return f"{line.trading_days} 个交易日内{reclaim}（{when}）"
        return f"到点执行：{when}"
    metric = METRIC_LABEL.get(line.metric or "", line.metric or DASH)
    op = OP_LABEL.get(line.op or "", line.op or DASH)
    threshold = _fmt(line.price) if line.price is not None else (
        f"参考价的 {_fmt(line.pct)}%" if line.pct is not None else DASH)
    confirm = (f"，连续 {line.confirm.bars} 根{'分钟' if line.confirm.basis == 'minute' else '日线'}确认"
               if line.confirm.bars > 1 else
               f"（{'分钟' if line.confirm.basis == 'minute' else '日线'}确认）")
    price_clause = f"{metric}{op} {threshold}{confirm}"
    if not line.extra:
        return price_clause
    return "、".join(EXTRA_LABEL.get(name, name) for name in line.extra) + f"，且{price_clause}"


def action_text(line: Line) -> str:
    template = ACTION_LABEL.get(line.action.type, line.action.type)
    return template.format(value=_fmt(line.action.value)) if "{value}" in template else template


def execution_text(line: Line) -> str:
    if line.execute_by == "time":
        return f"按时间：{EXECUTE_AT_LABEL.get(line.execute_at or '', line.execute_at or DASH)}"
    return f"按价格：{'分钟级' if line.confirm.basis == 'minute' else '日线级'}"


def _recomputed(line: Line) -> Any:
    if not line.derivation.formula.strip() or not line.derivation.inputs:
        return None
    try:
        return round(line.derivation.recompute(), 4)
    except FormulaError:
        return None


def _recomputed_action(line: Line) -> Any:
    if not line.derivation.action_formula.strip() or not line.derivation.action_inputs:
        return None
    try:
        return round(line.derivation.recompute_action(), 4)
    except FormulaError:
        return None


# --------------------------------------------------------------------------
# Structured rows shared by the Markdown card and the JSON payload
# --------------------------------------------------------------------------
def header_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    position = plan.position
    holding = (f"{position.quantity} 股（可卖 {position.sellable_quantity}），成本 "
               f"{_fmt(position.average_cost)}，市值 {_fmt(position.market_value)}"
               if position is not None else "空仓")
    return [
        {"key": "symbol", "label": "标的", "value": f"{plan.name} {plan.symbol}"},
        {"key": "plan_key", "label": "计划编号", "value": plan.plan_key},
        {"key": "plan_kind", "label": "计划类型", "value": PLAN_KIND_LABEL.get(plan.plan_kind, plan.plan_kind)},
        {"key": "stage", "label": "阶段", "value": f"{plan.stage}（{STAGE_LABEL.get(plan.stage, plan.stage)}）"},
        {"key": "status", "label": "状态", "value": STATUS_LABEL.get(plan.status, plan.status)},
        {"key": "position", "label": "持仓", "value": holding},
        {"key": "account_key", "label": "账户", "value": plan.account_key},
        {"key": "as_of_at", "label": "生成时间", "value": plan.as_of_at.isoformat()},
        {"key": "trading_date", "label": "计划交易日", "value": plan.trading_date.isoformat()},
        {"key": "valid_until", "label": "有效期至", "value": plan.valid_until.isoformat()},
        {"key": "template", "label": "模板",
         "value": plan.template_key if plan.template_key.endswith(plan.template_version)
         else f"{plan.template_key}（{plan.template_version}）"},
        {"key": "generator", "label": "生成器/报告版本",
         "value": f"{plan.generator_version} / {REPORT_VERSION}"},
        {"key": "inputs_hash", "label": "inputs_hash", "value": plan.inputs_hash},
        {"key": "supersedes_plan_id", "label": "替代的计划", "value": plan.supersedes_plan_id or DASH},
        {"key": "lowered_reason", "label": "止损下调理由", "value": plan.lowered_reason or DASH},
    ]


def _current_risk_pct(sizing: Any) -> Decimal:
    if sizing.current_risk_pct is not None:
        return sizing.current_risk_pct
    return (Decimal(sizing.current_shares) * sizing.stop_distance / sizing.equity * Decimal("100")).quantize(
        Decimal("0.01"))


def t1_locked_shares(plan: DisciplinePlan) -> int:
    """Shares held but not sellable on the plan's trading date (T+1), per a same-day snapshot."""
    recorded = (plan.metrics or {}).get("t1_locked_shares")
    if recorded is not None:
        return int(recorded)
    return t1_locked_shares_for(plan.position, plan.trading_date)


def _snapshot_stamp(plan: DisciplinePlan) -> str:
    """``MM-DD HH:MM`` of the broker snapshot the T+1 note is based on."""
    recorded = (plan.metrics or {}).get("t1_snapshot_at")
    if recorded:
        return str(recorded)[5:16].replace("T", " ")
    if plan.position is not None:
        return plan.position.observed_at.strftime("%m-%d %H:%M")
    return DASH


def sizing_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    sizing = plan.sizing
    if sizing is None:
        return []
    position = plan.position
    distance_pct = (sizing.stop_distance / sizing.reference_price * Decimal("100")).quantize(Decimal("0.01"))
    snapshot_exposure = (
        (position.market_value / sizing.equity * Decimal("100")).quantize(Decimal("0.01"))
        if position is not None and position.market_value is not None else None)
    return [
        {"key": "equity", "label": "账户权益 equity", "value": _fmt(sizing.equity)},
        {"key": "risk_per_trade_pct", "label": "单笔亏损容忍度（止损口径）", "value": _pct(sizing.risk_per_trade_pct)},
        # The open risk stays immediately under the budget it is judged against.
        {"key": "current_risk_pct", "label": "当前持仓风险 current_risk_pct = 持仓股数 × 止损距离 / equity",
         "value": _pct(_current_risk_pct(sizing))},
        {"key": "risk_policy", "label": "风险政策来源",
         "value": _risk_policy_text(sizing.risk_policy)},
        {"key": "risk_amount", "label": "风险预算 = equity × risk%", "value": _fmt(sizing.risk_amount)},
        {"key": "reference_price", "label": "参考价", "value": _fmt(sizing.reference_price)},
        {"key": "hard_stop", "label": "硬止损", "value": _fmt(sizing.hard_stop)},
        {"key": "stop_distance", "label": "止损距离",
         "value": f"{_fmt(sizing.stop_distance)}（{_fmt(distance_pct)}%）"},
        {"key": "sizing_price", "label": "定量价（本计划允许的最差成交价）",
         "value": _sizing_price_text(sizing)},
        {"key": "max_shares", "label": "风险上限 max_shares", "value": _fmt(sizing.max_shares)},
        {"key": "target_exposure_pct", "label": "集中度压力参考比例（不触发减仓）", "value": _pct(sizing.target_exposure_pct)},
        {"key": "cap_basis", "label": "压力测试依据",
         "value": _cap_basis_text(sizing.exposure_basis) if sizing.exposure_basis else DASH},
        {"key": "cap_shares", "label": "压力参考股数（仅提示）", "value": _fmt(sizing.cap_shares)},
        {"key": "tail_risk_estimated_loss_pct", "label": "当前仓位尾部情景估算损失",
         "value": _pct(sizing.tail_risk_estimated_loss_pct)},
        {"key": "binding_constraint", "label": "可执行股数约束",
         "value": f"仅按止损风险预算（{_pct(sizing.risk_per_trade_pct)}÷止损距离）"},
        {"key": "current_shares", "label": "当前持仓", "value": f"{_fmt(sizing.current_shares)} 股"},
        {"key": "sellable_quantity", "label": "当日可卖 sellable_quantity",
         "value": f"{_fmt(position.sellable_quantity)} 股" if position is not None else DASH},
        {"key": "current_exposure_pct", "label": "当前仓位比例（按参考价）",
         "value": _pct(sizing.current_exposure_pct)},
        {"key": "snapshot_exposure_pct", "label": "当前仓位比例（按快照市值 market_value / equity）",
         "value": _pct(snapshot_exposure)},
        {"key": "recommended_shares", "label": "止损风险允许股数 recommended_shares",
         "value": _fmt(sizing.recommended_shares)},
    ]


def entry_note(plan: DisciplinePlan) -> str | None:
    """How a new buy's entry price was chosen, in one recomputable sentence."""
    entry = (plan.metrics or {}).get("entry")
    if plan.plan_kind != "new_buy" or not isinstance(entry, dict):
        return None
    source = "最新收盘" if entry.get("entry_source") == "last_close" else "lane 结构参考价"
    return (f"入场参考价 {_fmt(entry.get('entry_price'))} = max(lane 结构参考价 {_fmt(entry.get('lane_reference'))}"
            f"（{entry.get('lane_reference_source')}），{entry.get('last_close_date')} 收盘 {_fmt(entry.get('last_close'))}"
            f"（{'盘中形成中' if entry.get('last_close_basis') == 'forming' else '已结算'}））；取{source}。"
            "硬止损、止损距离、股数、移动止损与减半仓均以它为基准。")


def recommendation_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    """The pool's human trigger/invalidation wording; research conditions, never system lines."""
    conditions = (plan.metrics or {}).get("recommendation_conditions")
    if not isinstance(conditions, dict):
        return []
    labels = (("trigger", "观察触发"), ("invalidation", "取消条件"), ("why_now", "为什么是现在"))
    return [{"key": key, "label": label, "value": str(conditions[key]),
             "note": str(conditions.get("note") or "研究条件，非系统线"),
             "decision_id": conditions.get("decision_id"), "as_of_date": conditions.get("as_of_date")}
            for key, label in labels if conditions.get(key)]


def _sizing_price_text(sizing: Any) -> str:
    """The price both share limits were computed at, and why it may differ.

    On a new buy this is the chase cap, not the entry reference: a budget that
    only holds at one point of a band the same card authorises is not a budget.
    """
    price = sizing.sizing_price
    if price is None or price == sizing.reference_price:
        return f"{_fmt(sizing.reference_price)}（同参考价）"
    distance = sizing.sizing_distance if sizing.sizing_distance is not None else price - sizing.hard_stop
    return (f"{_fmt(price)}（买入区间上沿=追高上限；止损距离 {_fmt(distance)}，"
            f"股数按此价计算，区间内任何价位买入都不超容忍度）")


def _risk_policy_text(policy: dict[str, Any] | None) -> str:
    """Say whether the number that sized this plan is the standing policy.

    Before the policy record existed, ``risk_per_trade_pct`` read the same way
    whether the user had chosen it or it was a module default nobody had ever
    put to them -- and for every card generated up to 2026-09-20 it was the
    latter.
    """
    if not policy:
        return "未记录（该计划早于风险政策记录）"
    source = str(policy.get("source") or "")
    applied = policy.get("applied_pct")
    standing = policy.get("per_name_loss_tolerance_pct")
    if source == "cli_override":
        return f"命令行覆盖 {applied}%（标准政策为 {standing}%，本卡未按标准政策生成）"
    return (f"用户设定 {standing}%（{policy.get('set_at')}），作为止损风险预算；允许高确信度重仓，"
            "集中度尾部压力仅提示，不因仓位比例本身减仓")


def _cap_basis_text(basis: dict[str, Any]) -> str:
    fallback = "，样本不足，按同板块全部阶段合并" if basis.get("fallback") else ""
    return (f"{basis.get('stage')} × {basis.get('board_label') or basis.get('board')}：两日最大跌幅 99% 分位 "
            f"{basis.get('q99_loss_pct')}%（{basis.get('samples')} 个样本{fallback}），"
            f"压力参考 = {basis.get('tolerance_pct')}% ÷ {basis.get('q99_loss_pct')}% 向下取 5 的倍数 = {basis.get('cap_pct')}%；"
            f"校准 {basis.get('calibration_version')}")


def sizing_notes(plan: DisciplinePlan) -> list[str]:
    """Sentences printed under the sizing table: the new-buy entry basis and the T+1 lock."""
    notes: list[str] = []
    entry = entry_note(plan)
    if entry:
        notes.append(entry)
    locked = t1_locked_shares(plan)
    if locked > 0:
        notes.append(f"生成日不可卖 {locked} 股（T+1，按 {_snapshot_stamp(plan)} 快照），价格线自下一交易日起可执行。")
    return notes


def omitted_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    """Lines the template refused to draw, with the rule and the numbers behind it."""
    rows: list[dict[str, Any]] = []
    for item in (plan.metrics or {}).get("omitted_lines") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or DASH)
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        rows.append({"kind": kind, "kind_label": KIND_LABEL.get(kind, kind),
                     "reason": str(item.get("reason") or DASH),
                     "inputs": {key: inputs[key] for key in sorted(inputs)}})
    return rows


def line_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    """Every line, in execution priority order, with its plain-Chinese wording."""
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(plan.lines, start=1):
        rows.append({
            "index": index, "kind": line.kind, "kind_label": KIND_LABEL.get(line.kind, line.kind),
            "label": line.label, "condition": condition_text(line), "action": action_text(line),
            "action_type": line.action.type,
            "action_value": None if line.action.value is None else str(line.action.value),
            "price": None if line.price is None else str(line.price),
            "pct": None if line.pct is None else str(line.pct),
            "metric": line.metric, "op": line.op,
            "confirm_bars": line.confirm.bars, "confirm_basis": line.confirm.basis,
            "extra": list(line.extra), "execute_by": line.execute_by, "execute_at": line.execute_at,
            "execution": execution_text(line), "trading_days": line.trading_days,
            "priority": line.priority, "rule_id": line.derivation.rule_id,
        })
    return rows


def _compared_value(line: Line) -> tuple[str, Any]:
    """What ``formula`` recomputes: the price, or the share count of a price-less time line.

    Mirrors ``quality._derivation_problem`` so the card's ``一致`` column and
    the gate's verdict can never disagree about the same line.
    """
    if line.price is not None:
        return "price", line.price
    if line.action.value is not None:
        return "action_value", line.action.value
    return "price", None


def derivation_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    """The audit table: rule, recorded inputs, formula and the recomputed value."""
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(plan.lines, start=1):
        recomputed = _recomputed(line)
        action_recomputed = _recomputed_action(line)
        compared_to, compared = _compared_value(line)
        rows.append({
            "index": index, "kind": line.kind, "kind_label": KIND_LABEL.get(line.kind, line.kind),
            "rule_id": line.derivation.rule_id,
            "inputs": {key: line.derivation.inputs[key] for key in sorted(line.derivation.inputs)},
            "formula": line.derivation.formula,
            "price": None if line.price is None else str(line.price),
            "recomputed": recomputed,
            "compared_to": compared_to,
            "compared_value": None if compared is None else str(compared),
            "matches": None if recomputed is None or compared is None
            else abs(recomputed - float(compared)) <= DERIVATION_TOLERANCE,
            "action_inputs": {key: line.derivation.action_inputs[key]
                              for key in sorted(line.derivation.action_inputs)},
            "action_formula": line.derivation.action_formula,
            "action_value": None if line.action.value is None else str(line.action.value),
            "action_recomputed": action_recomputed,
            "action_matches": None if action_recomputed is None or line.action.value is None
            else abs(action_recomputed - float(line.action.value)) <= DERIVATION_TOLERANCE,
        })
    return rows


def quality_rows(plan: DisciplinePlan) -> list[dict[str, Any]]:
    """Every declared check, whether or not the plan carries a verdict for it."""
    recorded = {check.check_id: check for check in plan.quality}
    ordered = [*CHECK_IDS, *[check_id for check_id in recorded if check_id not in CHECK_IDS]]
    rows: list[dict[str, Any]] = []
    for check_id in ordered:
        check = recorded.get(check_id)
        rows.append({
            "check_id": check_id, "label": CHECK_LABEL.get(check_id, check_id),
            "passed": None if check is None else check.passed,
            "detail": check.detail if check is not None else "本计划未记录该检查",
        })
    return rows


def evaluation_rows(evaluation: Evaluation | None) -> list[dict[str, Any]]:
    if evaluation is None:
        return []
    rows: list[dict[str, Any]] = []
    for state in evaluation.line_states:
        evidence = state.evidence or {}
        note = (evidence.get("not_evaluated") or evidence.get("voided_by")
                or evidence.get("rule") or evidence.get("confirmed_at") or "")
        rows.append({
            "kind": state.kind, "kind_label": KIND_LABEL.get(state.kind, state.kind), "label": state.label,
            "state": state.state, "state_label": LINE_STATE_LABEL.get(state.state, state.state),
            "basis": state.basis,
            "triggered_at": state.triggered_at.isoformat() if state.triggered_at else None,
            "trigger_price": None if state.trigger_price is None else str(state.trigger_price),
            "threshold": evidence.get("threshold"), "due": evidence.get("due"),
            "longest_streak": evidence.get("longest_streak"), "note": note,
        })
    return rows


def compliance_rows(records: list[ComplianceRecord] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records or []:
        deviation = record.deviation or {}
        rows.append({
            "verdict": record.verdict, "verdict_label": VERDICT_LABEL.get(record.verdict, record.verdict),
            "line_kind": record.line_kind,
            "line_kind_label": KIND_LABEL.get(record.line_kind or "", record.line_kind or DASH),
            "trade_record_id": record.trade_record_id,
            "time_expected": deviation.get("time_expected"), "time_actual": deviation.get("time_actual"),
            "price_expected": deviation.get("price_expected"), "price_actual": deviation.get("price_actual"),
            "price_diff": deviation.get("price_diff"), "price_diff_pct": deviation.get("price_diff_pct"),
            "quantity_expected": deviation.get("quantity_expected"),
            "quantity_actual": deviation.get("quantity_actual"),
            "quantity_diff": deviation.get("quantity_diff"),
            "trading_days_diff": deviation.get("trading_days_diff"),
            "notes": record.notes,
        })
    return rows


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def plan_payload(plan: DisciplinePlan, *, evaluation: Evaluation | None = None,
                 compliance: list[ComplianceRecord] | None = None,
                 plan_id: str | None = None) -> dict[str, Any]:
    """The JSON card: the full plan plus every projection the Markdown prints."""
    failed = [check.check_id for check in plan.quality if not check.passed]
    return {
        "report_version": REPORT_VERSION,
        "contract_version": plan.contract_version,
        "plan_id": plan_id,
        "plan_key": plan.plan_key,
        "symbol": plan.symbol,
        "name": plan.name,
        "account_key": plan.account_key,
        "trading_date": plan.trading_date.isoformat(),
        "status": plan.status,
        "stage": plan.stage,
        "quality_passed": not failed,
        "boundary": "research_only_human_decision_support",
        "header": header_rows(plan),
        "sizing": sizing_rows(plan),
        "sizing_notes": sizing_notes(plan),
        "t1_locked_shares": t1_locked_shares(plan),
        "lines": line_rows(plan),
        "omitted_lines": omitted_rows(plan),
        "recommendation_conditions": recommendation_rows(plan),
        "derivations": derivation_rows(plan),
        "quality": {"passed": not failed, "failed": failed, "checks": quality_rows(plan)},
        "evidence_refs": list(plan.evidence_refs),
        "evaluation": None if evaluation is None else {
            "as_of_at": evaluation.as_of_at.isoformat(), "basis": evaluation.basis,
            "plan_state": evaluation.plan_state,
            "plan_state_label": PLAN_STATE_LABEL.get(evaluation.plan_state, evaluation.plan_state),
            "inputs_hash": evaluation.inputs_hash, "line_states": evaluation_rows(evaluation),
        },
        "compliance": compliance_rows(compliance),
        "plan": plan.model_dump(mode="json"),
    }


def render_markdown(plan: DisciplinePlan, *, evaluation: Evaluation | None = None,
                    compliance: list[ComplianceRecord] | None = None,
                    plan_id: str | None = None) -> str:
    """The human-facing discipline card.  Deterministic for a given plan."""
    out: list[str] = [f"# 交易纪律卡 · {plan.name} {plan.symbol}", "", f"> {RESEARCH_NOTICE}", ""]
    out.extend(f"- {row['label']}：{row['value']}" for row in header_rows(plan))
    if plan_id:
        out.append(f"- plan_id：{plan_id}")

    out += ["", "## 一、仓位", ""]
    sizing = sizing_rows(plan)
    if sizing:
        out += _table(["项目", "数值"], [[row["label"], row["value"]] for row in sizing])
        notes = sizing_notes(plan)
        if notes:
            out.append("")
            out.extend(f"> {note}" for note in notes)
    else:
        out.append("本计划没有 sizing（无权益或无参考价）。")

    out += ["", "## 二、纪律线", ""]
    out += _table(["#", "类型", "条件", "动作", "执行", "优先级", "rule_id"],
                  [[row["index"], row["kind_label"], row["condition"], row["action"],
                    row["execution"], row["priority"], row["rule_id"]] for row in line_rows(plan)])
    out += ["", "原文表述：", ""]
    out.extend(f"{row['index']}. **{row['kind_label']}**：{row['label']}" for row in line_rows(plan))

    out += ["", "未生成的线及原因：", ""]
    omitted = omitted_rows(plan)
    if omitted:
        out += _table(["类型", "原因", "inputs"],
                      [[row["kind_label"], row["reason"], _canonical(row["inputs"])] for row in omitted])
    else:
        out.append("- 无（模板中的每条可选线都已生成）")

    conditions = recommendation_rows(plan)
    if conditions:
        out += ["", f"推荐池研究条件（{conditions[0]['note']}，决策 {str(conditions[0]['decision_id'] or '')[:12]}）：", ""]
        out.extend(f"- {row['label']}：{row['value']}" for row in conditions)

    out += ["", "## 三、推导表（每个价格与动作值都可复算；无价格的时间线复算的是股数）", ""]
    out += _table(["#", "类型", "rule_id", "inputs", "formula", "价格/股数", "复算值", "一致",
                   "action_formula", "动作值", "动作复算值", "一致"],
                  [[row["index"], row["kind_label"], row["rule_id"], _canonical(row["inputs"]),
                    row["formula"], row["compared_value"], row["recomputed"],
                    DASH if row["matches"] is None else ("是" if row["matches"] else "否"),
                    (f"{row['action_formula']} over {_canonical(row['action_inputs'])}"
                     if row["action_formula"] else DASH),
                    row["action_value"], row["action_recomputed"],
                    DASH if row["action_matches"] is None else ("是" if row["action_matches"] else "否")]
                   for row in derivation_rows(plan)])

    out += ["", "## 四、质量门", ""]
    checks = quality_rows(plan)
    failed = [row["check_id"] for row in checks if row["passed"] is False]
    out.append(f"结论：{'全部通过' if not failed else '未通过 ' + str(len(failed)) + ' 项：' + '、'.join(failed)}")
    out.append("")
    out += _table(["检查", "结果", "说明"],
                  [[f"{row['label']}（{row['check_id']}）",
                    DASH if row["passed"] is None else ("通过" if row["passed"] else "未通过"),
                    row["detail"]] for row in checks])

    out += ["", "## 五、证据引用", ""]
    out.extend(f"- {ref}" for ref in plan.evidence_refs)
    if not plan.evidence_refs:
        out.append(f"- {DASH}")

    if evaluation is not None:
        out += ["", "## 六、最新评估", "",
                f"- 评估时间：{evaluation.as_of_at.isoformat()}（{evaluation.basis} 口径）",
                f"- 计划状态：{PLAN_STATE_LABEL.get(evaluation.plan_state, evaluation.plan_state)}",
                f"- 评估 inputs_hash：{evaluation.inputs_hash}", ""]
        out += _table(["类型", "状态", "口径", "触发时间", "触发价", "阈值", "说明"],
                      [[row["kind_label"], row["state_label"], row["basis"], row["triggered_at"],
                        row["trigger_price"], row["threshold"], row["note"]]
                       for row in evaluation_rows(evaluation)])

    if compliance:
        out += ["", "## 七、成交对账", ""]
        out += _table(["判定", "线", "成交号", "计划价", "成交价", "价差", "计划时间", "成交时间",
                       "交易日差", "数量差", "说明"],
                      [[row["verdict_label"], row["line_kind_label"], row["trade_record_id"],
                        row["price_expected"], row["price_actual"], row["price_diff"],
                        row["time_expected"], row["time_actual"], row["trading_days_diff"],
                        row["quantity_diff"], row["notes"]] for row in compliance_rows(compliance)])

    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def slug(value: str) -> str:
    """A Windows-safe file stem; ``plan_key`` carries ``:`` which NTFS forbids."""
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)


def report_paths(output_root: Any, plan: DisciplinePlan) -> dict[str, Path]:
    """``<output_root>/<trading_date>/<symbol>-<plan_key>.{md,json}``."""
    directory = Path(output_root) / plan.trading_date.isoformat()
    stem = f"{plan.symbol}-{slug(plan.plan_key)}"
    return {"directory": directory, "markdown": directory / f"{stem}.md", "json": directory / f"{stem}.json"}


def write_report(plan: DisciplinePlan, *, output_root: Any, evaluation: Evaluation | None = None,
                 compliance: list[ComplianceRecord] | None = None,
                 plan_id: str | None = None) -> dict[str, Any]:
    """Write both cards and report where they landed.  The only I/O in here."""
    paths = report_paths(output_root, plan)
    paths["directory"].mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(plan, evaluation=evaluation, compliance=compliance, plan_id=plan_id)
    payload = plan_payload(plan, evaluation=evaluation, compliance=compliance, plan_id=plan_id)
    paths["markdown"].write_text(markdown, encoding="utf-8")
    paths["json"].write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    return {"markdown_path": str(paths["markdown"]), "json_path": str(paths["json"]),
            "markdown_bytes": len(markdown.encode("utf-8")), "report_version": REPORT_VERSION}


__all__ = [
    "CHECK_LABEL", "EXTRA_LABEL", "KIND_LABEL", "METRIC_LABEL", "OP_LABEL", "REPORT_VERSION",
    "STAGE_LABEL", "STATUS_LABEL", "VERDICT_LABEL", "action_text", "compliance_rows", "condition_text",
    "derivation_rows", "entry_note", "evaluation_rows", "execution_text", "header_rows", "line_rows", "omitted_rows",
    "plan_payload", "quality_rows", "recommendation_rows", "render_markdown", "report_paths", "sizing_notes", "sizing_rows", "slug",
    "t1_locked_shares", "write_report",
]

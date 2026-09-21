"""Human-readable Feishu messages for discipline transitions."""

from __future__ import annotations

from typing import Any

from ..dashboard_links import market_decision_url
from ..feishu_card_v2 import (
    card as card_v2,
    collapsible_panel,
    content_panel,
    markdown,
    metric_strip,
    open_url_button,
    tag,
)
from .contracts import DisciplinePlan, Line, LineState
from ..intraday_advisory.presentation import ensure_readable_card, symbol_text


ACTION_TEXT = {
    "exit_all": "纪律动作：退出全部可卖仓位（人工确认后执行）",
    "reduce_to_shares": "纪律动作：将仓位降至 {value} 股（人工确认后执行）",
    "reduce_by_pct": "纪律动作：减仓 {value}%（人工确认后执行）",
    "move_stop_to": "纪律动作：把止损线上移到 {value}（只上移，人工确认）",
    "block_add": "纪律动作：停止加仓",
    "alert": "纪律动作：立即复核",
    "buy_up_to_shares": "纪律动作：最多买入至 {value} 股（仅候选，必须人工确认）",
}


def _value(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def render_discipline_alert(plan: DisciplinePlan, line: Line, state: LineState, *,
                            dashboard_url: str | None = None) -> str:
    title = "【纪律线已触发】" if state.state == "triggered" else "【买入触发已越过追高上限】"
    scope = "当前持仓" if plan.plan_kind == "holding" else "正式推荐候选"
    action = _action_text(plan, line, state)
    observed = state.triggered_at.isoformat() if state.triggered_at else str(state.evidence.get("as_of") or "—")
    trigger_detail = (
        f"到期规则：{state.evidence.get('rule') or line.execute_at or '按计划时间'}"
        if line.execute_by == "time"
        else (f"触发价：{_value(state.trigger_price)}｜确认：连续 {line.confirm.bars} 根"
              f"{'已完成分钟线' if line.confirm.basis == 'minute' else '已收盘日线'}")
    )
    parts = [
        f"{title} {plan.name} {plan.symbol}",
        f"范围：{scope}｜阶段：{plan.stage}",
        f"线：{line.label}",
        trigger_detail,
        f"时间：{observed}",
        action,
        "边界：研究与纪律提醒，不下单；请核对盘口、可卖数量和最新公告。",
    ]
    decision_url = market_decision_url(dashboard_url)
    if decision_url:
        parts.append(f"纪律页：{decision_url}")
    return "\n".join(parts)


def _action_text(plan: DisciplinePlan, line: Line, state: LineState) -> str:
    if plan.plan_kind != "holding":
        return ("操作提示：暂停新买，等待回落或重新满足推荐条件" if state.state == "capped" else
                "操作提示：候选条件已失效，暂停新买并等待重新确认")
    if state.state == "capped":
        return "操作提示：不追买，等待新计划或回落后重新评估"
    return ACTION_TEXT.get(line.action.type, "纪律动作：人工复核").format(value=_value(line.action.value))


def discipline_alert_card(plan: DisciplinePlan, line: Line, state: LineState, *,
                          dashboard_url: str | None = None) -> dict[str, Any]:
    holding = plan.plan_kind == "holding"
    high_risk = holding and state.state == "triggered" and line.kind in {"hard_stop", "time_stop"}
    title = "持仓纪律线触发" if holding else "推荐候选条件变化"
    observed = state.triggered_at.isoformat() if state.triggered_at else str(state.evidence.get("as_of") or "数据暂缺")
    if line.execute_by == "time":
        trigger = f"到期条件：{state.evidence.get('rule') or line.execute_at or '按计划时间'}"
    else:
        trigger = (f"触发价格 {_value(state.trigger_price)} 元；"
                   f"已按连续 {line.confirm.bars} 根{'分钟线' if line.confirm.basis == 'minute' else '日线'}确认")
    action = _action_text(plan, line, state).replace("纪律动作：", "").replace("操作提示：", "")
    urgency = "立即复核" if high_risk else "需要关注"
    color = "red" if high_risk else "orange"
    scope = "当前持仓" if holding else "推荐候选"
    elements = [
        content_panel(
            f"**发生了什么**\n{line.label}已触发。{trigger}",
            element_id="trigger_summary",
            color="grey",
        ),
        metric_strip([
            ("对象", scope),
            ("优先级", urgency),
        ], element_id="trigger_metrics"),
        content_panel(f"**现在怎么做**\n{action}", element_id="trigger_action", color=color),
        collapsible_panel(
            "确认边界",
            "请结合最新公告、盘口与可交易状态人工确认。系统只提醒，不会下单。",
            element_id="trigger_boundary",
        ),
        markdown(f"触发时间 {observed} · 系统不下单", size="notation"),
    ]
    decision_url = market_decision_url(dashboard_url)
    if decision_url:
        elements.append(open_url_button("查看纪律卡", decision_url, element_id="open_discipline"))
    result = card_v2(
        title=f"{title}｜{plan.name}",
        subtitle=symbol_text(plan.symbol, plan.name),
        summary=f"{urgency}：{plan.name} {line.label}已触发",
        template=color,
        tags=[tag(urgency, color=color), tag(scope, color="neutral")],
        elements=elements,
        card_url=decision_url,
    )
    ensure_readable_card(result)
    return result


__all__ = ["ACTION_TEXT", "discipline_alert_card", "render_discipline_alert"]

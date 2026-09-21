"""Human-readable Feishu messages for discipline transitions."""

from __future__ import annotations

from typing import Any

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
    if dashboard_url:
        parts.append(f"纪律页：{dashboard_url.rstrip('/')}/market-decision")
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
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if high_risk else "orange",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**{symbol_text(plan.symbol, plan.name)}**\n"
             f"**发生了什么**\n{line.label}已触发。{trigger}\n\n"
             f"**现在怎么做**\n{_action_text(plan, line, state).replace('纪律动作：', '').replace('操作提示：', '')}"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             f"触发时间 {observed}｜请结合最新公告与可交易状态人工确认｜系统不下单"}]},
        ],
    }
    if dashboard_url:
        card["elements"].append({"tag": "action", "actions": [{"tag": "button",
            "text": {"tag": "plain_text", "content": "查看纪律卡"}, "type": "primary",
            "url": dashboard_url.rstrip("/") + "/market-decision"}]})
    ensure_readable_card(card)
    return card


__all__ = ["ACTION_TEXT", "discipline_alert_card", "render_discipline_alert"]

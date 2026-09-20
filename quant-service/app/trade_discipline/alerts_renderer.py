"""Human-readable Feishu messages for discipline transitions."""

from __future__ import annotations

from typing import Any

from .contracts import DisciplinePlan, Line, LineState


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
    action = ("纪律动作：不追买，等待新计划或回落后重新评估" if state.state == "capped" else
              ACTION_TEXT.get(line.action.type, "纪律动作：人工复核").format(value=_value(line.action.value)))
    observed = state.triggered_at.isoformat() if state.triggered_at else str(state.evidence.get("as_of") or "—")
    parts = [
        f"{title} {plan.name} {plan.symbol}",
        f"范围：{scope}｜阶段：{plan.stage}",
        f"线：{line.label}",
        (f"触发价：{_value(state.trigger_price)}｜确认：连续 {line.confirm.bars} 根"
         f"{'已完成分钟线' if line.confirm.basis == 'minute' else '已收盘日线'}"),
        f"时间：{observed}",
        action,
        "边界：研究与纪律提醒，不下单；请核对盘口、可卖数量和最新公告。",
    ]
    if dashboard_url:
        parts.append(f"纪律页：{dashboard_url.rstrip('/')}/market-decision")
    return "\n".join(parts)


__all__ = ["ACTION_TEXT", "render_discipline_alert"]

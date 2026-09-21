"""Compact human-readable Feishu messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .rules import AdvisorySignal
from .presentation import (
    ensure_readable_card, humanize_text, metric_lines, role_text, state_text, symbol_text,
)


def render_signal(signal: AdvisorySignal, *, source: str) -> str:
    evidence = "；".join(metric_lines(signal.metrics))
    return (f"【盘中提醒｜{role_text(source)}】\n{symbol_text(signal.symbol, signal.name)}\n"
            f"发生了什么：{humanize_text(signal.summary)}\n关键证据：{evidence}\n"
            "下一步：先核对价格能否延续、板块是否同步；未确认前不追价。")


def signal_card(signal: AdvisorySignal, *, source: str,
                dashboard_url: str | None = None) -> dict[str, Any]:
    metrics = "\n".join(f"- {row}" for row in metric_lines(signal.metrics))
    action = "持仓请核对纪律线与板块承接" if source == "holding" else "先观察延续性，未确认前不追价"
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if source == "holding" and signal.severity == "high" else "orange",
                   "title": {"tag": "plain_text", "content":
                             f"盘中提醒｜{role_text(source)}｜{signal.name or signal.symbol}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**{symbol_text(signal.symbol, signal.name)}**\n"
             f"**发生了什么**\n{humanize_text(signal.summary)}\n\n**关键证据**\n{metrics}\n\n"
             f"**现在怎么做**\n{action}"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             f"数据时间 {signal.observed_at:%H:%M:%S}｜主动侧成交仅为成交代理｜研究提醒，不执行交易"}]},
        ],
    }
    if dashboard_url:
        card["elements"].append({"tag": "action", "actions": [{"tag": "button",
            "text": {"tag": "plain_text", "content": "打开决策工作台"}, "type": "primary",
            "url": dashboard_url.rstrip("/") + "/market-decision"}]})
    ensure_readable_card(card)
    return card


def render_analysis(provider: str, output: dict[str, Any], *, report_kind: str,
                    generated_at: datetime) -> str:
    title = {"fixed": "固定盘中汇报", "midday": "午盘汇报", "tail": "尾盘汇报",
             "event": "突发信号补充分析", "discipline": "纪律线补充分析"}.get(report_kind, "盘中分析")
    attention = "、".join(humanize_text(x) for x in output.get("attention_symbols") or []) or "暂无"
    guidance = "\n".join(f"- {humanize_text(item)}" for item in output.get("guidance") or []) or "- 暂无新增动作"
    risks = "\n".join(f"- {humanize_text(item)}" for item in output.get("risks") or []) or "- 暂无新增风险"
    return (f"【{title}｜{provider}】{generated_at:%H:%M}\n市场判断：{state_text(output.get('market_state'))}\n"
            f"{humanize_text(output.get('summary'))}\n重点：{attention}\n操作提示：\n{guidance}\n风险：\n{risks}")


def analysis_card(provider: str, output: dict[str, Any], *, report_kind: str,
                  generated_at: datetime, dashboard_url: str | None = None) -> dict[str, Any]:
    title = {"fixed": "固定盘中汇报", "midday": "午盘汇报", "tail": "尾盘汇报",
             "event": "突发信号补充分析", "discipline": "纪律线补充分析",
             "ten_minute": "十分钟状态变化"}.get(report_kind, "盘中分析")
    state = str(output.get("market_state") or "watch")
    template = {"risk": "red", "watch": "orange", "calm": "blue"}.get(state, "orange")
    attention = "、".join(humanize_text(x) for x in output.get("attention_symbols") or []) or "暂无"
    guidance = "\n".join(f"- {humanize_text(item)}" for item in output.get("guidance") or []) or "- 暂无新增动作"
    risks = "\n".join(f"- {humanize_text(item)}" for item in output.get("risks") or []) or "- 暂无新增风险"
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text",
                   "content": f"{title}｜{provider}｜{generated_at:%H:%M}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**市场判断：{state_text(state)}**\n{humanize_text(output.get('summary'))}\n\n**重点对象：** {attention}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**建议**\n{guidance}\n\n**风险**\n{risks}"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             f"数据时间 {generated_at:%H:%M:%S}｜研究提醒，不执行交易"}]},
        ],
    }
    if dashboard_url:
        card["elements"].append({"tag": "action", "actions": [{"tag": "button",
            "text": {"tag": "plain_text", "content": "打开决策工作台"}, "type": "primary",
            "url": dashboard_url.rstrip("/") + "/market-decision"}]})
    ensure_readable_card(card)
    return card


__all__ = ["analysis_card", "render_analysis", "render_signal", "signal_card"]

"""Compact human-readable Feishu messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .rules import AdvisorySignal


def render_signal(signal: AdvisorySignal, *, source: str) -> str:
    role = {"holding": "持仓", "recommendation": "推荐池", "market_index": "大盘指数",
            "sector": "行业板块"}.get(source, source)
    metrics = "；".join(f"{key}={value}" for key, value in signal.metrics.items())
    notice = ("来源：Longhu 核心指数分钟数据；本通知不执行交易。" if source == "market_index" else
              "来源：东财行业板块同源快照；净流为辅助证据；本通知不执行交易。" if source == "sector" else
              "说明：主动买卖方向仅为成交侧代理，不代表机构身份；本通知不执行交易。")
    return (f"【盘中即时提醒｜{role}】\n{signal.name or signal.symbol} {signal.symbol}\n"
            f"{signal.summary}\n证据：{metrics}\n"
            f"{notice}")


def signal_card(signal: AdvisorySignal, *, source: str) -> dict[str, Any]:
    role = {"holding": "持仓", "recommendation": "推荐池", "market_index": "大盘指数",
            "sector": "行业板块"}.get(source, source)
    metrics = "\n".join(f"- **{key}**：{value}" for key, value in signal.metrics.items())
    notice = ("Longhu 核心指数分钟数据；仅供市场环境研究，不执行交易。" if source == "market_index" else
              "东财行业板块同源快照；净流仅作辅助证据，不执行交易。" if source == "sector" else
              "主动买卖方向仅为成交侧代理，不代表机构身份；仅供研究，不执行交易。")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if signal.severity == "high" else "orange",
                   "title": {"tag": "plain_text", "content": f"盘中即时提醒｜{role}｜{signal.name or signal.symbol}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**{signal.symbol}**\n{signal.summary}\n\n{metrics}"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             notice}]},
        ],
    }


def render_analysis(provider: str, output: dict[str, Any], *, report_kind: str,
                    generated_at: datetime) -> str:
    title = {"fixed": "固定盘中汇报", "midday": "午盘汇报", "tail": "尾盘汇报",
             "event": "突发信号补充分析", "discipline": "纪律线补充分析"}.get(report_kind, "盘中分析")
    attention = "、".join(output.get("attention_symbols") or []) or "无"
    guidance = "\n".join(f"- {item}" for item in output.get("guidance") or []) or "- 暂无新增建议"
    risks = "\n".join(f"- {item}" for item in output.get("risks") or []) or "- 未发现新增风险"
    return (f"【{title}｜{provider}】{generated_at:%H:%M}\n状态：{output.get('market_state','watch')}\n"
            f"{output.get('summary','')}\n关注：{attention}\n建议：\n{guidance}\n风险：\n{risks}\n"
            "仅供研究，不下单。")


def analysis_card(provider: str, output: dict[str, Any], *, report_kind: str,
                  generated_at: datetime) -> dict[str, Any]:
    title = {"fixed": "固定盘中汇报", "midday": "午盘汇报", "tail": "尾盘汇报",
             "event": "突发信号补充分析", "discipline": "纪律线补充分析",
             "ten_minute": "十分钟状态变化"}.get(report_kind, "盘中分析")
    state = str(output.get("market_state") or "watch")
    template = {"risk": "red", "watch": "orange", "calm": "blue"}.get(state, "orange")
    attention = "、".join(output.get("attention_symbols") or []) or "无"
    guidance = "\n".join(f"- {item}" for item in output.get("guidance") or []) or "- 暂无新增建议"
    risks = "\n".join(f"- {item}" for item in output.get("risks") or []) or "- 未发现新增风险"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text",
                   "content": f"{title}｜{provider}｜{generated_at:%H:%M}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**状态：{state}**\n{output.get('summary','')}\n\n**关注：** {attention}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**建议**\n{guidance}\n\n**风险**\n{risks}"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "仅供研究，不下单。"}]},
        ],
    }


__all__ = ["analysis_card", "render_analysis", "render_signal", "signal_card"]

"""Compact human-readable Feishu messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .rules import AdvisorySignal
from .presentation import ensure_readable_card, humanize_text, metric_lines, role_text, state_text, symbol_text


PROVIDER_TEXT = {"deepseek": "DeepSeek", "codex": "Codex"}
REPORT_TITLES = {
    "fixed": "盘中半小时复核", "midday": "午盘复核", "tail": "尾盘复核",
    "event": "异动补充分析", "discipline": "纪律线补充分析",
    "ten_minute": "盘中重要变化",
}


def _focus_lines(items: Any) -> list[str]:
    rows: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        label = symbol_text(str(item.get("symbol") or ""), str(item.get("name") or "") or None)
        status = humanize_text(item.get("status"))
        evidence = humanize_text(item.get("evidence"))
        action = humanize_text(item.get("action"))
        main = f"- **{label}**｜{status or '继续观察'}"
        detail = "；".join(part for part in (evidence, f"应对：{action}" if action else "") if part)
        rows.append(f"{main}\n  {detail}" if detail else main)
    return rows


def _structured_sections(output: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    holdings = _focus_lines(output.get("holding_focus"))
    recommendations = _focus_lines(output.get("recommendation_focus"))
    legacy: list[str] = []
    if not holdings and not recommendations:
        attention = [humanize_text(item) for item in output.get("attention_symbols") or []]
        guidance = [humanize_text(item) for item in output.get("guidance") or []]
        legacy = [f"- **{item}**" for item in attention]
        legacy.extend(f"- {item}" for item in guidance)
    return holdings, recommendations, legacy


def render_signal(signal: AdvisorySignal, *, source: str) -> str:
    evidence = "；".join(metric_lines(signal.metrics))
    action = "核对纪律线和板块承接" if source == "holding" else "观察延续性，未确认前不追价"
    return (f"【异动提醒｜{role_text(source)}】{symbol_text(signal.symbol, signal.name)}\n"
            f"{humanize_text(signal.summary)}\n证据：{evidence}\n应对：{action}")


def signal_card(signal: AdvisorySignal, *, source: str,
                dashboard_url: str | None = None) -> dict[str, Any]:
    metrics = "\n".join(f"- {row}" for row in metric_lines(signal.metrics))
    action = {
        "holding": "核对纪律线与板块承接",
        "recommendation": "观察延续性，未确认前不追价",
        "market_index": "确认波动是否扩散至多数指数与主线板块",
        "sector": "确认板块广度与持续性，不据单次快照追涨",
    }.get(source, "先观察后续确认")
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if source == "holding" and signal.severity == "high" else "orange",
                   "title": {"tag": "plain_text", "content":
                             f"异动提醒｜{role_text(source)}｜{signal.name or signal.symbol}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**{symbol_text(signal.symbol, signal.name)}**\n{humanize_text(signal.summary)}"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**证据**\n{metrics}"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**应对**　{action}"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             f"{signal.observed_at:%H:%M:%S}｜内外盘仅表示成交方向，不代表主力资金｜不执行交易"}]},
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
    title = REPORT_TITLES.get(report_kind, "盘中分析")
    headline_source = (output.get("notification_reason") if report_kind == "ten_minute" else None)
    headline = humanize_text(headline_source or output.get("headline") or output.get("summary"))
    market = humanize_text(output.get("market_summary") or output.get("summary"))
    holdings, recommendations, legacy = _structured_sections(output)
    sections = [f"【{title}】{generated_at:%H:%M}", f"大盘：{state_text(output.get('market_state'))}｜{headline}", market]
    if holdings:
        sections.append("持仓关注：\n" + "\n".join(holdings))
    if recommendations:
        sections.append("推荐池关注：\n" + "\n".join(recommendations))
    if legacy:
        sections.append("重点：\n" + "\n".join(legacy))
    risks = [humanize_text(item) for item in output.get("risks") or []]
    if risks:
        sections.append("风险：\n" + "\n".join(f"- {item}" for item in risks))
    sections.append(f"复核：{PROVIDER_TEXT.get(provider, provider)}")
    return "\n".join(part for part in sections if part)


def analysis_card(provider: str, output: dict[str, Any], *, report_kind: str,
                  generated_at: datetime, dashboard_url: str | None = None) -> dict[str, Any]:
    title = REPORT_TITLES.get(report_kind, "盘中分析")
    state = str(output.get("market_state") or "watch")
    template = {"risk": "red", "watch": "orange", "calm": "blue"}.get(state, "orange")
    headline_source = (output.get("notification_reason") if report_kind == "ten_minute" else None)
    headline = humanize_text(headline_source or output.get("headline") or output.get("summary")) or "盘面暂无显著变化"
    market = humanize_text(output.get("market_summary") or output.get("summary")) or "暂无新增市场证据"
    holdings, recommendations, legacy = _structured_sections(output)
    risks = [humanize_text(item) for item in output.get("risks") or []]
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text",
                   "content": f"{title}｜{generated_at:%H:%M}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**大盘｜{state_text(state)}**\n**{headline}**\n{market}"}},
        ],
    }
    if holdings:
        card["elements"].extend([{"tag": "hr"}, {"tag": "div", "text": {
            "tag": "lark_md", "content": "**持仓关注**\n" + "\n".join(holdings)}}])
    if recommendations:
        card["elements"].extend([{"tag": "hr"}, {"tag": "div", "text": {
            "tag": "lark_md", "content": "**推荐池关注**\n" + "\n".join(recommendations)}}])
    if legacy:
        card["elements"].extend([{"tag": "hr"}, {"tag": "div", "text": {
            "tag": "lark_md", "content": "**需要关注**\n" + "\n".join(legacy)}}])
    if risks:
        card["elements"].append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**风险边界**\n" + "\n".join(f"- {item}" for item in risks)}})
    card["elements"].append({"tag": "note", "elements": [{"tag": "plain_text", "content":
        f"{generated_at:%H:%M:%S}｜{PROVIDER_TEXT.get(provider, provider)} 复核｜研究提醒，不执行交易"}]})
    if dashboard_url:
        card["elements"].append({"tag": "action", "actions": [{"tag": "button",
            "text": {"tag": "plain_text", "content": "打开决策工作台"}, "type": "primary",
            "url": dashboard_url.rstrip("/") + "/market-decision"}]})
    ensure_readable_card(card)
    return card


__all__ = ["analysis_card", "render_analysis", "render_signal", "signal_card"]

"""Compact human-readable Feishu messages."""

from __future__ import annotations

from datetime import datetime
import re
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


def _focus_entries(items: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        entries.append({
            "label": symbol_text(str(item.get("symbol") or ""), str(item.get("name") or "") or None),
            "status": humanize_text(item.get("status")) or "继续观察",
            "evidence": humanize_text(item.get("evidence")) or "本轮没有新增确认信号",
            "action": humanize_text(item.get("action")) or "等待下一次确认",
        })
    return entries


def _focus_panel(entry: dict[str, str], *, role: str, index: int) -> dict[str, Any]:
    role_label = "持仓" if role == "holding" else "候选"
    role_color = "orange" if role == "holding" else "blue"
    content = (
        f"<text_tag color='{role_color}'>{role_label}</text_tag> **{entry['label']}**\n"
        f"{entry['status']}\n"
        f"**依据**　{entry['evidence']}\n"
        f"**应对**　{entry['action']}"
    )
    return content_panel(content, element_id=f"{role}_{index}", color="grey")


def _legacy_focus(text: str, *, role: str) -> dict[str, str]:
    prefix = "持仓" if role == "holding" else "推荐标的"
    value = text.removeprefix(prefix).strip()
    match = re.match(r"(?P<name>.+?)(?=现价|当前|今日|$)", value)
    name = (match.group("name") if match else value.split("；", 1)[0]).strip(" ：｜")
    clauses = [part.strip() for part in re.split(r"[；。]", value) if part.strip()]
    evidence = clauses[0] if clauses else value
    remaining = "；".join(clauses[1:]) if len(clauses) > 1 else "继续观察，等待下一次确认"
    if re.search(r"涨(?:9\.\d+|10(?:\.0+)?)%", evidence):
        status = "盘中涨停或接近涨停，观察封板承接"
    elif "跌" in evidence:
        status = "盘中回撤，等待重新确认"
    else:
        status = "盘中波动，继续观察"
    return {"label": name or "观察对象", "status": status, "evidence": evidence, "action": remaining}


def _legacy_entries(output: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    holdings: list[dict[str, str]] = []
    recommendations: list[dict[str, str]] = []
    other: list[str] = []
    for value in output.get("guidance") or []:
        text = humanize_text(value)
        if not text:
            continue
        if text.startswith("持仓"):
            holdings.append(_legacy_focus(text, role="holding"))
        elif text.startswith("推荐标的"):
            recommendations.append(_legacy_focus(text, role="recommendation"))
        else:
            other.append(text)
    if not holdings and not recommendations and not other:
        other = [humanize_text(item) for item in output.get("attention_symbols") or []]
    return holdings, recommendations, other


def _primary_action(state: str, holdings: list[dict[str, str]],
                    recommendations: list[dict[str, str]]) -> str:
    if holdings:
        return holdings[0]["action"]
    if state == "risk":
        return "先核对风险线与可交易状态，暂停新增仓位。"
    if recommendations:
        return recommendations[0]["action"]
    if state == "calm":
        return "按原计划观察，不因单次波动临时追价。"
    return "先确认指数与主线板块是否共振，没有确认信号不追价。"


def render_signal(signal: AdvisorySignal, *, source: str) -> str:
    if signal.kind == 'pressure_change':
        from .presentation import pressure_evidence
        previous = signal.metrics.get('previous_pressure_text')
        change = f"{previous} → {signal.metrics['pressure_text']}" if previous else signal.metrics['pressure_text']
        return f"【重要变化｜{symbol_text(signal.symbol,signal.name)}】\n{signal.summary}\n{pressure_evidence(signal.metrics)}\n{change}"
    evidence = "；".join(metric_lines(signal.metrics))
    action = "核对纪律线和板块承接" if source == "holding" else "观察延续性，未确认前不追价"
    return (f"【异动提醒｜{role_text(source)}】{symbol_text(signal.symbol, signal.name)}\n"
            f"{humanize_text(signal.summary)}\n证据：{evidence}\n应对：{action}")


def signal_card(signal: AdvisorySignal, *, source: str,
                dashboard_url: str | None = None) -> dict[str, Any]:
    if signal.kind == 'pressure_change':
        from .presentation import pressure_evidence, pressure_details
        previous = signal.metrics.get('previous_pressure_text')
        change = f"此前：{previous}\n现在：{signal.metrics['pressure_text']}" if previous else signal.metrics['pressure_text']
        elements = [content_panel(f"**{signal.summary}**\n{pressure_evidence(signal.metrics)}",element_id='change',color='grey'),
                    markdown(change),
                    collapsible_panel('区间证据与盘口辅助',pressure_details(signal.metrics),element_id='evidence'),
                    markdown(f"行情截至 {signal.observed_at:%H:%M:%S} · 描述性研究提醒，不是买卖指令",size='notation')]
        url = market_decision_url(dashboard_url)
        if url:
            elements.append(open_url_button('查看原观察条件',url,element_id='open_dashboard'))
        result = card_v2(title=f"重要变化｜{signal.name or signal.symbol.split('.')[0]}",
                         subtitle=f"{role_text(source)} · {signal.observed_at:%H:%M:%S}",
                         summary=f"{signal.name}：{signal.summary}",template='red' if signal.severity=='high' and signal.direction=='down' else 'orange',
                         tags=[],elements=elements,card_url=url)
        ensure_readable_card(result)
        return result
    metrics = "\n".join(f"- {row}" for row in metric_lines(signal.metrics))
    action = {
        "holding": "核对纪律线与板块承接",
        "recommendation": "观察延续性，未确认前不追价",
        "market_index": "确认波动是否扩散至多数指数与主线板块",
        "sector": "确认板块广度与持续性，不据单次快照追涨",
    }.get(source, "先观察后续确认")
    decision_url = market_decision_url(dashboard_url)
    severity_text = "高优先级" if signal.severity == "high" else "需要关注"
    severity_color = "red" if signal.severity == "high" else "orange"
    elements = [
        content_panel(
            f"**发生了什么**\n{humanize_text(signal.summary)}" + (
                '\n量能：缺少可比区间成交额，暂不判断放缩量' if source in {'market_index','sector'} else ''),
            element_id="event_summary",
            color="grey",
        ),
        metric_strip([
            ("对象", role_text(source)),
            ("级别", severity_text),
        ], element_id="event_metrics"),
        content_panel(f"**现在怎么做**\n{action}", element_id="event_action", color="orange"),
        collapsible_panel("行情证据", metrics, element_id="event_evidence"),
        markdown(
            f"{signal.observed_at:%H:%M:%S} · 内外盘仅表示成交方向，不代表主力资金 · 系统不下单",
            size="notation",
        ),
    ]
    if decision_url:
        elements.append(open_url_button("打开决策工作台", decision_url, element_id="open_dashboard"))
    result = card_v2(
        title=f"异动提醒｜{signal.name or symbol_text(signal.symbol)}",
        subtitle=f"{role_text(source)} · {signal.observed_at:%H:%M:%S}",
        summary=f"{severity_text}：{signal.name or symbol_text(signal.symbol)} {humanize_text(signal.summary)}",
        template=severity_color,
        tags=[tag(severity_text, color=severity_color), tag(role_text(source), color="neutral")],
        elements=elements,
        card_url=decision_url,
    )
    ensure_readable_card(result)
    return result


def render_analysis(provider: str, output: dict[str, Any], *, report_kind: str,
                    generated_at: datetime) -> str:
    if report_kind in {'event','discipline','ten_minute'} and 'delta_items' in output:
        sections = ['【重要变化补充】'+humanize_text(output.get('headline'))]
        for item in output['delta_items']:
            sections.append(f"{symbol_text(item['symbol'],item['name'])}｜{item['change']}\n{item['evidence']}\n观察条件：{item['action']}")
        if output.get('market_delta'):
            sections.append(humanize_text(output['market_delta']))
        return '\n'.join(sections)
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
    if report_kind in {'event','discipline','ten_minute'} and 'delta_items' in output:
        elements = []
        for index,item in enumerate(output['delta_items']):
            elements.append(content_panel(
                f"**{symbol_text(item['symbol'],item['name'])}｜{item['change']}**\n{item['evidence']}\n**条件影响**　{humanize_text(item['action'])}",
                element_id=f'delta_{index}',color='grey'))
        if output.get('market_delta'):
            elements.append(markdown(humanize_text(output['market_delta'])))
        elements.append(markdown(f"证据截至 {str(output.get('data_as_of') or '')[11:19]} · 生成 {generated_at:%H:%M:%S} · {PROVIDER_TEXT.get(provider,provider)} 复核",size='notation'))
        result = card_v2(title='重要变化补充',subtitle=f'{generated_at:%H:%M}',
                         summary=humanize_text(output.get('headline')) or '观察条件发生变化',
                         template='orange',tags=[],elements=elements,card_url=market_decision_url(dashboard_url))
        ensure_readable_card(result)
        return result
    title = REPORT_TITLES.get(report_kind, "盘中分析")
    state = str(output.get("market_state") or "watch")
    template = {"risk": "red", "watch": "orange", "calm": "blue"}.get(state, "orange")
    headline_source = (output.get("notification_reason") if report_kind == "ten_minute" else None)
    headline = humanize_text(headline_source or output.get("headline") or output.get("summary")) or "盘面暂无显著变化"
    market = humanize_text(output.get("market_summary") or output.get("summary")) or "暂无新增市场证据"
    holding_entries = _focus_entries(output.get("holding_focus"))
    recommendation_entries = _focus_entries(output.get("recommendation_focus"))
    legacy_holdings, legacy_recommendations, legacy_other = _legacy_entries(output)
    if not holding_entries:
        holding_entries = legacy_holdings
    if not recommendation_entries:
        recommendation_entries = legacy_recommendations
    risks = [humanize_text(item) for item in output.get("risks") or []]
    decision_url = market_decision_url(dashboard_url)
    action = _primary_action(state, holding_entries, recommendation_entries)
    state_color = {"risk": "red", "watch": "orange", "calm": "green"}.get(state, "orange")
    elements: list[dict[str, Any]] = [
        content_panel(
            f"<text_tag color='{state_color}'>大盘结论</text_tag> **{headline}**\n{market}",
            element_id="market_conclusion",
            color="grey",
        ),
        metric_strip([
            ("市场状态", state_text(state)),
            ("持仓关注", str(len(holding_entries))),
            ("推荐池关注", str(len(recommendation_entries))),
        ], element_id="attention_metrics"),
        content_panel(f"**现在怎么做**\n{action}", element_id="primary_action", color=state_color),
    ]
    if holding_entries:
        elements.append(markdown("### 当前持仓", margin="4px 0 0 0"))
        elements.extend(_focus_panel(item, role="holding", index=index)
                        for index, item in enumerate(holding_entries[:2], 1))
        if len(holding_entries) > 2:
            extra = "\n\n".join(
                f"**{item['label']}**｜{item['status']}\n应对：{item['action']}"
                for item in holding_entries[2:]
            )
            elements.append(collapsible_panel(
                f"其余持仓关注（{len(holding_entries) - 2}）", extra, element_id="holding_more"))
    if recommendation_entries:
        elements.append(markdown("### 推荐候选", margin="4px 0 0 0"))
        elements.extend(_focus_panel(item, role="recommendation", index=index)
                        for index, item in enumerate(recommendation_entries[:2], 1))
        if len(recommendation_entries) > 2:
            extra = "\n\n".join(
                f"**{item['label']}**｜{item['status']}\n应对：{item['action']}"
                for item in recommendation_entries[2:]
            )
            elements.append(collapsible_panel(
                f"其余候选关注（{len(recommendation_entries) - 2}）", extra,
                element_id="recommend_more"))
    if legacy_other:
        elements.append(collapsible_panel(
            f"补充观察（{len(legacy_other)}）", "\n\n".join(legacy_other),
            element_id="legacy_more"))
    if risks:
        elements.append(collapsible_panel(
            f"风险与数据边界（{len(risks)}）", "\n".join(f"- {item}" for item in risks),
            element_id="risk_details"))
    elements.append(markdown(
        f"生成时间 {generated_at:%H:%M:%S} · {PROVIDER_TEXT.get(provider, provider)} 复核 · 研究提醒，不执行交易",
        size="notation",
    ))
    if decision_url:
        elements.append(open_url_button("打开决策工作台", decision_url, element_id="open_dashboard"))
    result = card_v2(
        title=title,
        subtitle=f"{generated_at:%Y-%m-%d %H:%M} · {PROVIDER_TEXT.get(provider, provider)}",
        summary=f"{state_text(state)}：{headline}",
        template=template,
        tags=[
            tag(state_text(state), color=state_color),
            tag(f"持仓 {len(holding_entries)}", color="orange"),
            tag(f"候选 {len(recommendation_entries)}", color="blue"),
        ],
        elements=elements,
        card_url=decision_url,
    )
    ensure_readable_card(result)
    return result


__all__ = ["analysis_card", "render_analysis", "render_signal", "signal_card"]

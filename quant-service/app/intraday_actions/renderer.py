"""Presentation-only manual action cards; never execute or infer fills."""
from __future__ import annotations

from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

from ..dashboard_links import market_decision_url
from ..feishu_card_v2 import card, content_panel, markdown, collapsible_panel, open_url_button
from ..intraday_advisory.presentation import ensure_readable_card, symbol_text
from .contracts import LABELS


def _number(value):
    try:
        result = float(value)
        return f'{result:.2f}' if isfinite(result) else '未提供'
    except (TypeError, ValueError):
        return '未提供'


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return '时间口径待核对'
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return '未提供'


def action_card(event: dict, *, dashboard_url: str | None = None,
                simulation: bool = False) -> dict:
    action = event.get('action')
    if action not in LABELS:
        raise ValueError('unknown action cannot be presented as a trade reminder')
    label = LABELS[action]
    leg = event.get('leg')
    second = leg in (2, 'second', 'second_leg')
    if action == 't_buy_first':
        label = '做T第二步：卖出旧仓' if second else '做T第一步：先买入'
    elif action == 't_sell_first':
        label = '做T第二步：接回' if second else '做T第一步：先卖出'
    quantity = event.get('quantity')
    shares = f'{int(quantity)} 股' if isinstance(quantity, (int, float)) and isfinite(quantity) and quantity > 0 else '数量待核实'
    limits = event.get('price_range')
    if isinstance(limits, dict):
        limits = [limits.get('low'), limits.get('high')]
    zone = '区间未提供，不能据此报价'
    buying = action in {'first_buy', 'add'} or (action == 't_buy_first' and not second) or (action == 't_sell_first' and second)
    if not buying and event.get('reference') is not None:
        zone = f'触发参考位 {_number(event["reference"])} 元；非保证成交价'
    if isinstance(limits, (list, tuple)) and len(limits) == 2:
        zone = f'{_number(limits[0])}–{_number(limits[1])} 元'
    if not buying and event.get('min_sell_price') is not None:
        zone += f'；最低允许卖价 {_number(event["min_sell_price"])} 元'
    elements = [content_panel(
        f'**{label} · {shares}**\n{"参考区间" if buying else "价格条件"}：{zone}\n最新价：{_number(event.get("price"))} 元',
        element_id='action', color='grey')]
    reasons = event.get('reasons') or []
    elements.append(markdown('**确认依据**\n' + '\n'.join(f'- {str(x)}' for x in reasons[:3])))
    boundary = '这是条件提醒，不代表已成交；下单前核对最新行情及实际可用资金。'
    if action.startswith('t_'):
        boundary += '\n第二步必须对应已核实的第一步成交；价差可能消失，不保证能够接回或卖出。'
    locked = event.get('locked_quantity', 0)
    if isinstance(locked, (int, float)) and isfinite(locked) and locked > 0:
        boundary += f'\n当日锁定 {int(locked)} 股，不能当日卖出。'
    if event.get('hard_stop') is not None:
        boundary += f'\n原失效位：{_number(event["hard_stop"])} 元，不因做T下移。'
    elements.append(content_panel(boundary, element_id='boundary', color='grey'))
    elements.append(collapsible_panel('数据时点',
        f'确认分钟：{_time(event.get("evidence_at"))}\n行情：{_time(event.get("quote_at"))}\n账户：{_time(event.get("account_at"))}',
        element_id='timing'))
    url = market_decision_url(dashboard_url)
    if url:
        elements.append(open_url_button('查看计划与走势', url, element_id='dashboard'))
    prefix = '本地模拟｜' if simulation else '条件确认｜'
    result = card(title=prefix + symbol_text(event.get('symbol', ''), event.get('name')),
                  subtitle=label, summary=f'{prefix}{event.get("name", "")}：{label}，{shares}',
                  template='red' if action == 'exit' else 'blue', tags=[],
                  elements=elements, card_url=url)
    ensure_readable_card(result)
    return result

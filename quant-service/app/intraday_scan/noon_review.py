"""Point-in-time company-review gate for a single intraday scan.

This is a separate analyst artifact. It never changes the scan, formal pool,
broker holdings, or a trading plan. A complete report needs every target in
the scan's deterministic research floor, not just the easiest company.
"""
from __future__ import annotations

from datetime import datetime

from .presentation import build as build_presentation
from .reports import STATES
from .rules import digest

MARKET_FIELDS = ('breadth', 'sector_rotation', 'turnover_flow', 'news', 'afternoon_scenarios')
COMPANY_FIELDS = ('business', 'fundamentals', 'catalyst', 'price_volume',
                  'sector_relative', 'peer_comparison', 'risk', 'why_now',
                  'trigger', 'invalidation')
DISPOSITIONS = {'consider': '条件满足时考虑', 'watch': '继续观察', 'avoid': '本轮回避'}


def _timestamp(value, label):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f'{label} must include timezone')
    return parsed


def _filled(record, fields, label):
    for field in fields:
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f'{label}.{field} is required')


def validate(result, receipt, review):
    """Return coverage for a bound review; reject fabricated completeness."""
    if review.get('run_id') != receipt.get('run_id') or review.get('cutoff') != result.get('cutoff'):
        raise ValueError('Review must bind to the exact scan run_id and cutoff')
    if receipt.get('database_readback') is not True:
        raise ValueError('Scan database readback is required')
    if receipt.get('result_hash') != digest(result):
        raise ValueError('Scan result does not match the receipt')
    completed_at = _timestamp(review.get('completed_at'), 'completed_at')
    cutoff = _timestamp(result['cutoff'], 'cutoff')
    if (cutoff.hour, cutoff.minute) != (11, 30):
        raise ValueError('This review contract accepts the 11:30 noon cutoff only')
    if completed_at < cutoff:
        raise ValueError('Review cannot complete before its market cutoff')
    market = review.get('market')
    if not isinstance(market, dict):
        raise ValueError('market review is required')
    _filled(market, MARKET_FIELDS, 'market')
    _filled(review, ('comparison_summary', 'limitations'), 'review')
    presentation = result.get('presentation') or build_presentation(
        [row for lane in result['lanes'] for row in lane['items']], result['lanes'])
    plan = presentation.get('research_plan') or build_presentation(
        [row for lane in result['lanes'] for row in lane['items']], result['lanes'])['research_plan']
    required = {row['symbol'] for row in plan['targets']}
    companies = review.get('companies')
    if not isinstance(companies, list):
        raise ValueError('companies must be a list')
    seen = set()
    for company in companies:
        symbol = company.get('symbol')
        if symbol in seen:
            raise ValueError(f'Duplicate company review: {symbol}')
        seen.add(symbol)
        if symbol not in required:
            raise ValueError(f'Company outside this run research plan: {symbol}')
        _filled(company, COMPANY_FIELDS, symbol)
        if company.get('disposition') not in DISPOSITIONS:
            raise ValueError(f'{symbol}.disposition is invalid')
        references = company.get('source_refs')
        if not isinstance(references, list) or not references or not all(
                isinstance(ref, str) and ref.strip() for ref in references):
            raise ValueError(f'{symbol}.source_refs must contain evidence references')
        evidence_at = _timestamp(company.get('evidence_available_at'), f'{symbol}.evidence_available_at')
        if evidence_at > completed_at:
            raise ValueError(f'{symbol} review uses evidence unavailable when completed')
    return dict(status='complete' if required == seen else 'partial',
                required=len(required), reviewed=len(seen), missing=sorted(required - seen),
                plan=plan, presentation=presentation)


def render(result, receipt, review):
    coverage = validate(result, receipt, review)
    targets = {row['symbol']: row for row in coverage['plan']['targets']}
    companies = {row['symbol']: row for row in review['companies']}
    ordered = [companies[row['symbol']] for row in coverage['plan']['targets'] if row['symbol'] in companies]
    priority = {'consider': 0, 'watch': 1, 'avoid': 2}
    ordered.sort(key=lambda row: (priority[row['disposition']],
                                  list(targets).index(row['symbol'])))
    lines = ['# 午盘决策复核', '', '## 结论与重点股票', '']
    if coverage['status'] != 'complete':
        lines += [f"本轮公司研究未闭环：已复核 {coverage['reviewed']}/{coverage['required']} 只；"
                  f"缺少 {', '.join(coverage['missing'])}。未复核标的不能给出买入结论。", '']
    else:
        lines += [f"本轮最低研究范围 {coverage['required']} 只均已复核；以下是有条件的研究判断，"
                  '不是自动买入指令，也不改变正式推荐池。', '']
    lines += ['|股票|本轮判断|为什么现在看|下午触发条件|取消/失效|主要风险|',
              '|---|---|---|---|---|---|']
    for row in ordered:
        target = targets[row['symbol']]
        parts = [f"{target['name']}（{row['symbol'].split('.')[0]}）", DISPOSITIONS[row['disposition']],
                 row['why_now'], row['trigger'], row['invalidation'], row['risk']]
        lines.append('|' + '|'.join(part.replace('|', '/').replace('\n', ' ') for part in parts) + '|')
    lines += ['', '## 原正式推荐续审', '',
              '|原优先级|股票|午盘结构|本轮研究判断|', '|---|---|---|---|']
    for formal in coverage['presentation']['formal_recommendations']:
        company = companies.get(formal['symbol'])
        verdict = DISPOSITIONS[company['disposition']] if company else '未完成本轮公司复核'
        lines.append(f"|{formal.get('recommendation_priority', '—')}|"
                     f"{formal['name']}（{formal['symbol'].split('.')[0]}）|"
                     f"{STATES.get(formal.get('state'), '证据待确认')}|{verdict}|")
    lines += ['', '## 逐股复核与同类比较', '']
    for row in ordered:
        target = targets[row['symbol']]
        timing = ('午盘后补充' if _timestamp(row['evidence_available_at'], 'evidence_available_at')
                  > _timestamp(result['cutoff'], 'cutoff') else '截至扫描截点已知')
        lines += [f"### {target['name']}（{row['symbol'].split('.')[0]}）", '',
                  f"- 入选依据：{'；'.join(target['selection_reasons'])}。",
                  f"- 主营与经营：{row['business']}；{row['fundamentals']}。",
                  f"- 催化与量价：{row['catalyst']}；{row['price_volume']}。",
                  f"- 板块与同类：{row['sector_relative']}；{row['peer_comparison']}。",
                  f"- 风险与失效：{row['risk']}；{row['invalidation']}。",
                  f"- 证据时点：{row['evidence_available_at']}（{timing}）；来源：{'；'.join(row['source_refs'])}。", '']
    market = review['market']
    lines += ['## 市场、板块与下午情景', '',
              f"- 广度：{market['breadth']}", f"- 板块轮动：{market['sector_rotation']}",
              f"- 成交与资金：{market['turnover_flow']}", f"- 消息传导：{market['news']}",
              f"- 下午情景：{market['afternoon_scenarios']}",
              f"- 跨策略取舍：{review['comparison_summary']}", '']
    lines += ['## 九策略当轮摘要', '',
              '|策略|本轮匹配|本轮仍可观察前排|数据状态|', '|---|---:|---|---|']
    for lane in result['lanes']:
        front = coverage['presentation']['strategy_front'].get(lane['key'], [])
        names = '、'.join(f"{row['name']}（{row['symbol'].split('.')[0]}）" for row in front) or '无'
        lines.append(f"|{lane['label']}|{lane.get('total_matches', '—')}|{names}|{lane.get('status', '—')}|")
    lines += ['## 范围与审计', '',
              f"- 研究状态：{coverage['status']}；已复核 {coverage['reviewed']}/{coverage['required']}。",
              f"- 行情截止：{result['cutoff']}；研究完成：{review['completed_at']}。",
              f"- 运行 ID：{receipt['run_id']}；结果哈希：{receipt['result_hash']}。",
              f"- 限制：{review['limitations']}",
              '- 本报告独立于正式推荐池、实际持仓与下单；未结算的当日量价及资金均需下午或收盘复核。', '']
    return '\n'.join(lines), coverage

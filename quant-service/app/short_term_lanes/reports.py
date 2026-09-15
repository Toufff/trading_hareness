"""One coherent report bundle: independent strategies first, overview second."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .report_parts import candidates, context, limits, review_sections, stock
from .report_results import lane_result, result_lines
from .tracking_report import sections as tracking_sections
from ..effectiveness.report import sections as effectiveness_sections
from ..event_research.report import sections as event_sections

VERSION = 'strategy-report-bundle-results-first-2026-09-11'


def lane_review(result: dict, lane: dict) -> dict:
    """Re-scope shared company evidence without importing another lane's rank."""
    symbols = {r['symbol'] for r in lane['selected'] + lane.get('caution_list', [])}
    first = lane['selected'][0]['symbol'] if lane['selected'] else None
    groups = []
    for group in result.get('review_groups', []):
        items = []
        for review in group['items']:
            if review['symbol'] not in symbols:
                continue
            links = [m for m in review['memberships'] if m['lane'] == lane['key']]
            reason = '；'.join(f"{m['label']}{'候选' if m['list']=='selected' else '风险名单'}展示第{m['display_rank']}：{m['reason']}" for m in links)
            items.append({**review, 'memberships': links, 'selection_reason': reason})
        if items:
            key = 'company' if group['key'] in {'priority', 'additional'} else group['key']
            existing = next((g for g in groups if g['key'] == key), None)
            if existing is not None:
                existing['items'].extend(items)
            else:
                groups.append(dict(key=key, label='本策略公司复核' if key == 'company' else group['label'], items=items))
    for group in groups:
        group['items'].sort(key=lambda review: min(
            ((m['list'] != 'selected', m['display_rank']) for m in review['memberships']),
            default=(True, len(symbols) + 1)))
    targets = [{**r, 'selection_reason': f"{lane['label']}首位代表：{lane['selected'][0]['reason']}。",
                'memberships': [m for m in r['memberships'] if m['lane']==lane['key']]}
               for r in result.get('review_plan', []) if r['symbol']==first]
    reviewed = {r['symbol'] for g in groups if g['key']!='background' for r in g['items']}
    missing = [r['symbol'] for r in targets if r['symbol'] not in reviewed]
    return dict(review_policy=f"本报告独立讨论{lane['label']}，优先复核本策略展示首位；复用公司事实，不复用其他策略的名次或买入结论。",
                review_plan=targets, review_groups=groups,
                review_coverage=dict(planned=len(targets), completed=len(targets)-len(missing), missing_symbols=missing,
                                     selected_reviewed=len({r['symbol'] for r in lane['selected']} & reviewed), selected_total=len(lane['selected'])))


def overlaps(result: dict) -> list[dict]:
    symbols: dict[str, dict] = {}
    for lane in result['lanes']:
        for section in ('selected', 'caution_list'):
            for row in lane.get(section, []):
                entry = symbols.setdefault(row['symbol'], dict(symbol=row['symbol'], name=row['name'], memberships=[]))
                entry['memberships'].append(dict(key=lane['key'], label=lane['label'],
                                                state='候选' if section=='selected' else '风险观察', reason=row['reason']))
    result_rows = []
    for row in symbols.values():
        if len({m['key'] for m in row['memberships']}) < 2:
            continue
        states = {m['state'] for m in row['memberships']}
        row['interpretation'] = ('不同策略的候选/风险状态有分歧，保留各自条件，不相互抵消风险。' if len(states)>1 else
                                 '同一段行情满足多种结构；只保留一个股票身份，分别检查触发条件，不是独立利好计票。')
        result_rows.append(row)
    return result_rows


def overview(result: dict, strategy_reports: list[dict], repeated: list[dict]) -> str:
    lines = [f"# {result['as_of_date']} 短线策略总报告", '',
             '这里汇总各策略独立报告：先看研究结论与策略差异，需要细节时打开对应报告；不把所有策略分数拼成一个买入排名。', '']
    lines += ['## 本次结论', '']
    for report in strategy_reports:
        summary = report['result_summary']
        lines += [f"### [{summary['label']}]({report['filename']})", ''] + result_lines(summary, compact=True)
    lines += event_sections(result.get('event_research'))
    lines += ['## 策略对照与独立报告', '', '| 策略 | 匹配 / 展示 | 代表候选 | 本策略研究范围 |', '|---|---:|---|---|']
    for lane, report in zip(result['lanes'], strategy_reports):
        c = report['review']['review_coverage']
        lead = stock(lane['selected'][0]) if lane['selected'] else (
            '风险观察：' + stock(lane['caution_list'][0]) if lane.get('caution_list') else '无候选')
        lines += [f"| [{lane['label']}]({report['filename']}) | {lane['total_matches']} / {len(lane['selected'])} | {lead} | 首位复核 {c['completed']}/{c['planned']}，展示候选公司证据覆盖 {c['selected_reviewed']}/{c['selected_total']} |"]
    lines += ['', '## 跨策略重复与分歧', '', '重复出现不是独立利好计票，也不会自动提高仓位。', '']
    for row in repeated:
        lines += [f"- {stock(row)}："+'；'.join(f"{m['label']}（{m['state']}）" for m in row['memberships'])+f"。{row['interpretation']}"]
    if not repeated:
        lines += ['当前展示范围没有跨策略重复股票。']
    lines += ['', '## 各策略候选索引', '', '完整量价、公司证据、确认与放弃条件见各自独立报告；此处只用于快速定位。', '']
    for lane, report in zip(result['lanes'], strategy_reports):
        lines += [f"- [{lane['label']}]({report['filename']})：" + ('、'.join(stock(r) for r in lane['selected']) or lane['empty_reason'])]
    lines += ['','## 详细研究证据',''] + review_sections(result)
    lines += ['## 数据与筛选说明',''] + context(result)
    return '\n'.join(lines + effectiveness_sections(result.get('effectiveness')) + tracking_sections(result.get('followup')) + limits(result))


def make_bundle(result: dict) -> dict:
    day = result['as_of_date']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('Invalid report date')
    source = {k: v for k, v in result.items() if k != 'report_bundle'}
    digest = hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    summary_file = f'{day}_short_term_lanes.md'  # Keep existing report links valid.
    docs = []
    keys = set()
    for lane in result['lanes']:
        key = lane['key']
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key) or key in keys or key == 'overview':
            raise ValueError('Invalid or duplicate strategy report key')
        keys.add(key)
        review = lane_review(result,lane)
        summary = lane_result(result, lane, review)
        c = review['review_coverage']
        lines = [f"# {day} {lane['label']}独立报告", '', f"[返回总报告]({summary_file})", '',
                 '## 本次结论', ''] + result_lines(summary)
        lines += event_sections(result.get('event_research'), {s['symbol'] for s in lane['selected']+lane.get('caution_list',[])})
        lines += ['## 详细研究证据', ''] + review_sections(review) + candidates(lane)
        lines += ['## 数据与筛选说明', '', f"本策略要找什么：{lane['purpose']}。", ''] + context(result)
        lines += [f"公司证据覆盖：展示候选 {c['selected_reviewed']}/{c['selected_total']}；首位代表 {c['completed']}/{c['planned']}。范围分开计数，不把首位完成说成全部候选完成。", '']
        if key == 'event':
            lines += [f"已核验事件覆盖 {result['coverage'].get('verified_event_symbols',0)} 只；没有匹配不等于全市场没有事件。", '']
        lines += effectiveness_sections(result.get('effectiveness'),key) + tracking_sections(result.get('followup'),key) + limits(result)
        if key == 'accumulation':
            experiment = result.get('flow_sensitivity', {})
            if experiment.get('status') == 'shadow_only':
                lines += ['', '## 资金因子敏感性对照（未启用）', '', experiment['note'], '']
                labels = {'baseline':'原资金+横盘门槛', 'flow_gate_removed':'仅取消资金硬门槛、保留原分数',
                          'flow_15pct':'取消资金门槛、资金分占15%', 'no_flow':'取消资金门槛、只按横盘分'}
                for variant in experiment['variants']:
                    lines += [f"- {labels[variant['key']]}：{variant['eligible_count']}只；对照前列：" +
                              '、'.join(stock(r) for r in variant['top'][:5]) + '。']
            lines += ['','## 横盘观察档案（含低活跃与近似形态）','',
                      '此处保留形态发现，不把低成交额样本推荐为短线买入。研究结论与优先名单在上方。','']
            for row in result.get('accumulation_observations', []):
                lines += [f"- {stock(row)}：{'双条件符合' if row['matched_intersection'] else '近似形态'}；资金分{row['flow']['score']:.0f}、横盘分{row['sideways']['score']:.0f}；成交额{row['amount']/1e8:.2f}亿元；{'活跃门槛通过' if row['activity_eligible'] else '活跃不足，仅留历史观察'}。"]
            for field, label in [('flow','仅按资金条件扫描'),('sideways','仅按横盘条件扫描')]:
                items = result.get('accumulation_single_conditions', {}).get(field, [])
                lines += ['',f'## 附录：{label}（{len(items)}只）','', '、'.join(stock(r) for r in items) or '无匹配']
        docs.append(dict(key=key,title=f"{lane['label']}独立报告",filename=f'{day}_strategy_{key}.md',
                         markdown='\n'.join(lines),review=review,result_summary=summary))
    repeated = overlaps(result)
    summary = dict(key='overview',title='所有策略总报告',filename=summary_file,markdown=overview(result,docs,repeated))
    # Independent reports are built first; the catalog opens on the overview.
    reports = [summary, *docs]
    for doc in reports:
        doc.update(as_of_date=day,content_sha256=hashlib.sha256(doc['markdown'].encode()).hexdigest())
    return dict(version=VERSION,source_sha256=digest,overlaps=repeated,reports=reports)


def write_bundle(directory: Path, result: dict, bundle: dict) -> None:
    """Atomic per file; write the JSON manifest last so partial output is detectable."""
    for report in bundle['reports']:
        if Path(report['filename']).name != report['filename']:
            raise ValueError('Report filename must remain within the output directory')
    directory.mkdir(parents=True,exist_ok=True)
    def replace(filename: str, text: str) -> None:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',newline='',dir=directory,delete=False,suffix='.tmp') as handle:
            temporary = Path(handle.name)
            handle.write(text)
        try:
            os.replace(temporary,directory/filename)
        finally:
            temporary.unlink(missing_ok=True)
    for report in bundle['reports']:
        replace(report['filename'],report['markdown'])
    replace(f"{result['as_of_date']}_short_term_lanes.json",json.dumps({**result,'report_bundle':bundle},ensure_ascii=False,indent=2))

"""Result-first presentation, derived only from the same lane's saved evidence."""
from __future__ import annotations

from .report_parts import stock


def _cell(value: object) -> str:
    """Keep generated Markdown tables valid for human-authored evidence."""
    text = str(value or '').strip().replace('\r\n', '\n').replace('\r', '\n')
    return text.replace('|', '\\|').replace('\n', '<br>')


def lane_result(result: dict, lane: dict, review: dict) -> dict:
    selected = lane['selected']
    caution = lane.get('caution_list', [])
    shown = {r['symbol'] for r in selected + caution}
    discovered = [r for r in lane.get('observation_list', []) if r['symbol'] not in shown]
    if result['status'] != 'completed' or lane.get('status') == 'data_gap':
        conclusion = '数据不足，本轮不能给出有效筛选结论；下列已有样本不作为当前买入依据。'
    elif selected:
        conclusion = f"本轮有 {len(selected)} 只条件观察候选，另有 {len(caution)} 只风险观察；先看研究结论，再检查触发条件，不是立即买入名单。"
    elif caution:
        conclusion = f"本轮没有进入条件观察的候选；保留 {len(caution)} 只风险观察，当前不按本策略给出入场建议。{lane['empty_reason']}"
    else:
        conclusion = f"本轮无可展示候选。{lane['empty_reason']}"
    if lane.get('observation_list'):
        conclusion += f" 独立结构观察保留{len(lane['observation_list'])}只，不因接近涨停或市场风险路由删除，须分别查看风险与确认条件。"
    if lane['key'] == 'event':
        conclusion += f" 已核验事件覆盖 {result['coverage'].get('verified_event_symbols', 0)} 只，没有匹配不等于全市场没有事件。"
    evidence = {r['symbol']: r for g in review.get('review_groups', []) if g['key'] != 'background' for r in g['items']}
    rows = []
    for state, entries in [('结构观察', discovered), ('条件观察', selected), ('风险观察', caution)]:
        for row in entries:
            research = evidence.get(row['symbol'])
            if research:
                research_conclusion = f"{research['outcome_label']}：{research['conclusion']}"
            else:
                research_conclusion = None
            rows.append(dict(symbol=row['symbol'], name=row['name'], state=state,
                conclusion=research_conclusion,
                reason=row['reason'], confirmation=row.get('confirmation', ''),
                invalidation=row.get('invalidation', ''), caution=row.get('caution', ''),
                expiry=row.get('expiry', '')))
    return dict(key=lane['key'], label=lane['label'], conclusion=conclusion, rows=rows)


def result_lines(summary: dict, *, compact: bool = False) -> list[str]:
    lines = [summary['conclusion'], '']
    if not summary['rows']:
        return lines

    # The overview used to collapse unreviewed rows to bare stock names.  That
    # made a strategy screen look like an unexplained recommendation list and
    # happened to leave only the first reviewed representative with prose.
    # Keep company review and strategy evidence distinct, but show both for
    # every displayed row on the first screen.
    lines += [
        '| 股票 | 状态 | 为什么关注 | 公司复核 |',
        '|---|---|---|---|',
    ]
    for row in summary['rows']:
        review = row['conclusion'] or '未进入本轮公司深度复核；当前仅保留量价/结构筛选结论'
        lines.append(
            f"| {_cell(stock(row))} | {_cell(row['state'])} | {_cell(row['reason'])} | {_cell(review)} |"
        )

    lines += ['', '| 股票 | 确认条件 | 放弃条件 | 风险与有效期 |', '|---|---|---|---|']
    for row in summary['rows']:
        confirmation = row['confirmation'] or '本轮未形成入场条件'
        invalidation = row['invalidation'] or '本轮未形成失效条件'
        risk = f"{row['caution'] or '无额外说明'}；有效期：{row['expiry'] or '未注明'}"
        lines.append(
            f"| {_cell(stock(row))} | 确认条件：{_cell(confirmation)} | "
            f"放弃条件：{_cell(invalidation)} | {_cell(risk)} |"
        )
    lines.append('')
    return lines

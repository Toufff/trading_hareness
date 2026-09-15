"""Result-first presentation, derived only from the same lane's saved evidence."""
from __future__ import annotations

from .report_parts import stock


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
    decisions = {r['symbol']: r for r in (review.get('decision_research') or {}).get('dossiers', [])}
    rows = []
    for state, entries in [('结构观察', discovered), ('条件观察', selected), ('风险观察', caution)]:
        for row in entries:
            research = evidence.get(row['symbol'])
            decision = decisions.get(row['symbol'])
            if research:
                research_conclusion = f"{research['outcome_label']}：{research['conclusion']}"
            elif decision and decision.get('status') == 'passed':
                research_conclusion = f"决策门禁通过：{decision['conclusion']}"
            elif decision and decision.get('status') == 'rejected':
                research_conclusion = f"决策门禁否决：{decision['conclusion']}"
            elif decision:
                failed = '；'.join(g.get('conclusion','') for g in decision.get('gates', []) if g.get('verdict') == 'unknown')
                research_conclusion = f"决策门禁终止为证据不足：{failed or decision.get('conclusion','未形成可执行计划')}"
            else:
                research_conclusion = (
                    '本股仅属于该策略的量价筛选展示，未列入本轮优先决策研究；'
                    '因此没有买入结论，而不是留下待他人完成的任务。'
                )
            rows.append(dict(symbol=row['symbol'], name=row['name'], state=state,
                conclusion=research_conclusion,
                reason=row['reason'], confirmation=row.get('confirmation', ''),
                invalidation=row.get('invalidation', ''), caution=row.get('caution', ''),
                expiry=row.get('expiry', '')))
    return dict(key=lane['key'], label=lane['label'], conclusion=conclusion, rows=rows)


def result_lines(summary: dict, *, compact: bool = False) -> list[str]:
    lines = [summary['conclusion'], '']
    for row in summary['rows']:
        lines += [f"{'-' if compact else '###'} {stock(row)} · {row['state']}", '',
                  f"结论：{row['conclusion']}", '']
        if not compact:
            lines += [f"- 为什么关注：{row['reason']}",
                      f"- 确认条件：{row['confirmation'] or '本轮未形成入场条件'}",
                      f"- 放弃条件：{row['invalidation'] or '本轮未形成失效条件'}",
                      f"- 注意：{row['caution']}；有效期：{row['expiry']}", '']
    return lines

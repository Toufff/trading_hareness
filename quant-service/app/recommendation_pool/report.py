"""Render the persisted decision, never rerank it for a different surface."""


def markdown(bundle):
    lines = [f"# {bundle['as_of_date']} 推荐决策", '', bundle['market_assessment'], '',
             f"状态：{bundle['status']}；决策编号：{bundle['decision_id']}。", '', '## 本轮重点', '']
    for item in bundle['recommended']:
        lines += [f"### {item['priority']}. {item['name']}（{item['symbol'].split('.')[0]}）", '',
                  f"阶段：{item['stage']}。{item['why_now']}", '', f"为什么优先：{item['comparison']}", '',
                  f"同类比较：{item['peer_comparison']}", '', f"观察触发：{item['trigger']}", '',
                  f"取消条件：{item['invalidation']}", '', f"公司风险：{item['company_risk']}", '']
    lines += ['## 其余完成复核的结论', '']
    for item in bundle['reviewed']:
        if item['decision'] != 'recommend':
            lines += [f"- {item['name']}（{item['symbol'].split('.')[0]}）：{item['comparison']}；{item['invalidation']}"]
    c = bundle['coverage']
    lines += ['', '## 覆盖与证据', '', f"全量候选 {c['candidates']}，实际深入复核 {c['reviewed']}；必核缺项：{c['missing']}；错误：{c['errors']}。",
              '没有深入复核的候选保留在完整筛查账本，不视为排除。不把本报告称为全市场逐股公司尽调。', '']
    for item in bundle['reviewed']:
        lines.append(f"- {item['name']}：" + '；'.join(f"[{s['published_date']}]({s['url']})" for s in item['sources']))
    lines += ['', '## 完整候选索引（未复核不是排除）', '']
    lines += [f"- {s['name']}（{s['symbol']}） · {s['state']} · " + ' / '.join(f"{m['lane']} #{m['rank']}" for m in s['memberships']) for s in bundle['screening']]
    return '\n'.join(lines) + '\n'

"""Render one human-facing close report without reranking persisted evidence."""


def _stock_names(bundle, scan=None, names=None):
    rows = [*(bundle.get('reviewed') or []), *(bundle.get('screening') or [])]
    for lane in (scan or {}).get('lanes') or []:
        rows += lane.get('tracking_candidates') or []
    resolved = {row['symbol']: row.get('name') or row['symbol'] for row in rows}
    resolved.update({symbol: name for symbol, name in (names or {}).items() if name})
    return resolved


def _stocks(symbols, names):
    return '、'.join(f"{names.get(symbol, symbol)}（{symbol.split('.')[0]}）" for symbol in symbols) or '无'


def _pool_change_lines(bundle, scan=None, names=None):
    names = _stock_names(bundle, scan, names)
    baseline = bundle.get('baseline') or {}
    target = bundle.get('target_groups') or {}
    lines = [
        '## 推荐池更新', '',
        '这是内部研究池的正式目标状态，不代表同花顺已经同步，也不构成下单授权。', '',
        '| 分组 | 更新前 | 更新后 | 新增 | 移除 | 保留 |',
        '|---|---:|---:|---|---|---|',
    ]
    for group in ('推荐', '观察'):
        before = sorted(set(baseline.get(group) or []))
        after = sorted(set(target.get(group) or []))
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        retained = sorted(set(before) & set(after))
        lines.append(
            f"| {group} | {len(before)} | {len(after)} | {_stocks(added, names)} | "
            f"{_stocks(removed, names)} | {_stocks(retained, names)} |"
        )
    lines += ['', '| 分组 | 本轮完整名单 |', '|---|---|']
    for group in ('推荐', '观察'):
        lines.append(f"| {group} | {_stocks(sorted(set(target.get(group) or [])), names)} |")
    return lines


def _scan_lines(scan):
    if not scan:
        return ['## 九策略总扫描', '', '本次报告未附扫描快照；推荐池仍以决策编号绑定的扫描证据为准。', '']
    coverage = scan.get('coverage') or {}
    lines = [
        '## 九策略总扫描', '',
        f"扫描状态：{scan.get('status', 'unknown')}；股票池 {coverage.get('universe', '—')} 只，"
        f"完整历史 {coverage.get('complete_history', '—')} 只；去重候选 {len(bundle_symbols(scan))} 只。", '',
        '| 策略 | 全量匹配 | 条件观察 | 结构观察 | 风险观察 | 首位展示 |',
        '|---|---:|---:|---:|---:|---|',
    ]
    for lane in scan.get('lanes') or []:
        selected = lane.get('selected') or []
        observed = lane.get('observation_list') or []
        caution = lane.get('caution_list') or []
        displayed = selected or observed or caution
        lead = displayed[0] if displayed else None
        lead_text = f"{lead.get('name', lead['symbol'])}（{lead['symbol'].split('.')[0]}）" if lead else '无'
        lines.append(
            f"| {lane.get('label') or lane.get('key')} | {lane.get('total_matches', 0)} | "
            f"{len(selected)} | {len(observed)} | {len(caution)} | {lead_text} |"
        )
    return lines + ['']


def bundle_symbols(scan):
    return {
        row['symbol']
        for lane in scan.get('lanes') or []
        for section in ('tracking_candidates',)
        for row in lane.get(section) or []
    }


def markdown(bundle, scan=None, names=None):
    coverage = bundle.get('coverage') or {}
    target = bundle.get('target_groups') or {}
    lines = [f"# {bundle['as_of_date']} 盘后总扫描与推荐池更新", '', '## 一眼结论', '',
             bundle['market_assessment'], '',
             f"- 总扫描：九套策略已运行，形成 {coverage.get('candidates', '—')} 只去重候选；"
             f"深入复核 {coverage.get('reviewed', '—')} 只。",
             f"- 推荐池：{bundle['status']}；推荐 {len(target.get('推荐') or [])} 只，"
             f"观察 {len(target.get('观察') or [])} 只；决策编号 `{bundle['decision_id']}`。", '']
    lines += _pool_change_lines(bundle, scan, names)
    lines += ['', *_scan_lines(scan), '## 本轮重点', '']
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

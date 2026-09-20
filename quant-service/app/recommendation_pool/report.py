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


def _pool_change_lines(bundle, names):
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


def _pct(value):
    return f"{value:.1f}%" if isinstance(value, (int, float)) else '—'


def _share(value):
    return f"{value:.0%}" if isinstance(value, (int, float)) else '—'


def _yi(value):
    return f"{value / 1e8:+.2f}亿" if isinstance(value, (int, float)) else '—'


def _peer_position(peer):
    if not peer:
        return '—'
    lane = f"{peer.get('best_lane')} 第{peer.get('best_rank')}/{peer.get('best_population')}"
    if peer.get('scope') == 'global':
        return f"跨板块补位 · {peer.get('sector_label') or '未知板块'} · {lane}"
    return f"板块第{peer.get('sector_position')} · {lane}"


def _sector_overview_line(reference, note):
    """State the whole sector, not only the handful of candidates this scan found."""
    overview = reference.get('sector_overview')
    market = reference.get('market') or {}
    if not overview:
        proxy = ((note or {}).get('sector_view') or {}).get('proxy') or {}
        if proxy.get('kind') == 'etf':
            return f"- 板块整体：系统无该板块聚合，使用 ETF 代理 {proxy.get('name')}（{proxy.get('symbol')}）。"
        return '- 板块整体：系统无该板块聚合，本轮也没有登记 ETF 代理。'
    return (f"- 板块整体（系统，全市场 {overview.get('members')} 只成分）：{overview.get('label')} "
            f"10日涨幅中位 {_pct(overview.get('return10_median'))} vs 全市场 {_pct(market.get('median_return10'))}，"
            f"上涨占比 {_share(overview.get('up_fraction'))}，近3日广度 {_share(overview.get('recent_breadth'))}"
            f"（较前期 {_share(overview.get('breadth_acceleration'))}），3日资金 {_yi(overview.get('flow_3d'))}，"
            f"系统判定 {overview.get('relative_strength')}（参考标签，不是评分）。")


def _sector_view_lines(note):
    view = note.get('sector_view') or {}
    if not view:
        return ['- 板块判断：该决策早于板块整体判断合同。']
    proxy = view.get('proxy') or {}
    source = (f"ETF 代理 {proxy.get('name')}（{proxy.get('symbol')}）[{proxy.get('published_date')}]({proxy.get('url')})"
              if proxy.get('kind') == 'etf' else '系统板块成分聚合')
    lines = [f"- 板块判断（作者）：{view.get('assessment')}；依据：{source}。",
             f"- 板块走势：{view.get('trend')}",
             f"- 板块量价：{view.get('volume_price')}"]
    if note.get('weak_sector_entry_reason'):
        lines.append(f"- 弱板块仍入选的理由：{note['weak_sector_entry_reason']}")
    if note.get('sector_disagreement_reason'):
        lines.append(f"- 与系统判定不一致的理由：{note['sector_disagreement_reason']}")
    return lines


def _recommendation_note_lines(item, names):
    note = item.get('recommendation_note')
    reference = item.get('ranking_reference')
    if not note or not reference:
        return ['推荐说明：该历史决策早于完整推荐说明合同，未记录策略排名与同板块对照。', '']
    rankings = '；'.join(f"{r['lane']} 第{r['rank']}/{r['population']}" for r in reference['lane_rankings']) or '本轮九策略均未入选'
    position = (f"{reference['sector_label']} 候选第{reference['sector_position']}/{reference['sector_candidates']}"
                if reference.get('sector_position') else '无同板块候选排名')
    lines = ['#### 推荐说明', '',
             f"- 系统排名（由扫描生成，不是人工填写）：{rankings}；同板块位置：{position}。"]
    if reference.get('outranked_count'):
        lines.append(f"- 同板块有 {reference['outranked_count']} 只候选排在它前面。")
    lines.append(_sector_overview_line(reference, note))
    lines += _sector_view_lines(note)
    lines += [f"- 如何使用排名：{note['rank_assessment']}",
              f"- 最终入选理由：{note['entry_reason']}",
              f"- 优先级理由：{note['priority_reason']}"]
    if note.get('carry_over_reason'):
        lines.append(f"- 旧推荐续留理由：{note['carry_over_reason']}")
    required = {p['symbol']: p for p in reference['required_peers']}
    if note.get('sector_peers'):
        lines += ['', '| 同板块对手 | 系统位置 | 10日涨幅 | 5日净流入占比 | 必答 | 为什么不选 |', '|---|---|---:|---:|---|---|']
        for peer in note['sector_peers']:
            fact = required.get(peer['symbol'])
            metrics = (fact or {}).get('metrics') or {}
            duty = {'sector': '是', 'global': '补位'}.get((fact or {}).get('scope'), '否')
            lines.append(f"| {names.get(peer['symbol'], peer['symbol'])}（{peer['symbol'].split('.')[0]}） | "
                         f"{_peer_position(fact)} | {_pct(metrics.get('return_10d'))} | "
                         f"{_pct(metrics.get('net5_amount_pct'))} | {duty} | {peer['why_not']} |")
    lines += ['', '| 额外信息核查 | 发现 | 来源 |', '|---|---|---|']
    for check in note['information_checks']:
        lines.append(f"| {check['topic']} | {check['finding']} | [{check['published_date']}]({check['url']}) |")
    return lines + ['']


def markdown(bundle, scan=None, names=None):
    # One name resolution for the whole report; the same mapping is reused by
    # every section instead of being rebuilt per recommended stock.
    resolved = _stock_names(bundle, scan, names)
    coverage = bundle.get('coverage') or {}
    target = bundle.get('target_groups') or {}
    lines = [f"# {bundle['as_of_date']} 盘后总扫描与推荐池更新", '', '## 一眼结论', '',
             bundle['market_assessment'], '',
             f"- 总扫描：九套策略已运行，形成 {coverage.get('candidates', '—')} 只去重候选；"
             f"深入复核 {coverage.get('reviewed', '—')} 只。",
             f"- 推荐池：{bundle['status']}；推荐 {len(target.get('推荐') or [])} 只，"
             f"观察 {len(target.get('观察') or [])} 只；决策编号 `{bundle['decision_id']}`。", '']
    lines += _pool_change_lines(bundle, resolved)
    lines += ['', *_scan_lines(scan), '## 本轮重点', '']
    for item in bundle['recommended']:
        lines += [f"### {item['priority']}. {item['name']}（{item['symbol'].split('.')[0]}）", '',
                  f"所属行业：{item.get('sector') or '本轮未登记'}。", '',
                  f"主营与经营：{item.get('business') or '本轮正式研究缺少主营与经营说明'}", '',
                  f"阶段：{item['stage']}。{item['why_now']}", '', f"为什么优先：{item['comparison']}", '',
                  f"同类比较：{item['peer_comparison']}", '', f"观察触发：{item['trigger']}", '',
                  f"取消条件：{item['invalidation']}", '', f"公司风险：{item['company_risk']}", '']
        lines += _recommendation_note_lines(item, resolved)
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

"""Deterministic presentation projection for one immutable intraday result."""

from .rules import STATE_ORDER

ACTIONABLE = {'confirmed_observation', 'platform_observation'}


def _compact(row):
    keys = (
        'symbol', 'name', 'lane', 'source', 'state', 'matched_today', 'formal_state',
        'formal_rank', 'price', 'change_pct', 'amount', 'turnover', 'reference',
        'support', 'vwap', 'reason', 'current_reason', 'entry_scenario', 'evidence_gaps',
        'recommendation_priority', 'recommendation_decision_id', 'recommendation_data_date',
        'recommendation_valid_until', 'recommendation_trigger', 'recommendation_invalidation',
        'recommendation_comparison', 'recommendation_why_now',
    )
    return {key: row.get(key) for key in keys if row.get(key) is not None}


def build(items, lanes):
    """Produce the only allowed human-summary selection.

    Formal recommendation priority is not mixed with strategy ranks.  Strategy
    candidates are selected only within their own lanes using the already
    deterministic lane ordering from the engine.
    """
    formal = {}
    for row in items:
        if not row.get('formal_recommendation'):
            continue
        old = formal.get(row['symbol'])
        key = (row.get('recommendation_priority', 9999), STATE_ORDER.get(row['state'], 999), row['lane'])
        old_key = ((old or {}).get('recommendation_priority', 9999),
                   STATE_ORDER.get((old or {}).get('state'), 999), (old or {}).get('lane', ''))
        if old is None or key < old_key:
            formal[row['symbol']] = row
    formal_rows = sorted(formal.values(), key=lambda r: (r.get('recommendation_priority', 9999), r['symbol']))

    strategy_followups = {}
    new_discoveries = {}
    strategy_front = {}
    research_targets = {}
    for row in formal_rows:
        research_targets[row['symbol']] = dict(symbol=row['symbol'], name=row['name'],
            selection_reasons=['昨日正式推荐需要按本轮行情重新复核'], representative_lanes=[],
            comparison_peer=None)
    for lane in lanes:
        ordered = lane['items']
        strategy_followups[lane['key']] = [_compact(r) for r in ordered
            if r['source'] == 'previous' and not r.get('formal_recommendation')
            and not r.get('matched_today')][:3]
        new_discoveries[lane['key']] = [_compact(r) for r in ordered
            if r['source'] != 'previous'][:3]
        # A still-valid historical watch is not a fresh match.  Keep it in
        # strategy_followups, never in the current front-runners list.
        strategy_front[lane['key']] = [_compact(r) for r in ordered
            if r.get('matched_today') and r['state'] in ACTIONABLE][:3]
        # Match the post-close research floor: one live representative from
        # every non-empty lane, deduplicated across lanes, plus carried formal
        # recommendations.  The queue is not a new recommendation ranking.
        matched = [r for r in ordered if r.get('matched_today')
            and r['state'] not in {'invalidated', 'data_gap'}]
        if matched:
            lead = matched[0]
            peer = next((r for r in matched[1:] if r['symbol'] != lead['symbol']), None)
            target = research_targets.setdefault(lead['symbol'], dict(
                symbol=lead['symbol'], name=lead['name'], selection_reasons=[],
                representative_lanes=[], comparison_peer=None))
            target['selection_reasons'].append(f"{lane['label']}本轮首位：{lead.get('current_reason') or lead.get('reason') or '本轮策略匹配'}")
            target['representative_lanes'].append(lane['key'])
            if peer and not target['comparison_peer']:
                target['comparison_peer'] = dict(symbol=peer['symbol'], name=peer['name'], lane=lane['key'])
    return {
        'selection_contract': (
            '正式推荐按正式priority；历史候选和新发现只按各自策略内的状态、正式排名、成交额和代码稳定排序；'
            '不同策略分数不跨策略比较；不得从混合表人工挑选。'
        ),
        'formal_recommendations': [_compact(r) for r in formal_rows],
        'strategy_followups': strategy_followups,
        'new_discoveries': new_discoveries,
        'strategy_front': strategy_front,
        'research_plan': dict(status='pending_company_review', target_count=len(research_targets),
                              targets=list(research_targets.values()),
                              scope='正式推荐续审与各非空策略本轮首位；同组第二名作比较；并非新买授权'),
    }

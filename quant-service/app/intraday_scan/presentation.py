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
    for lane in lanes:
        ordered = lane['items']
        strategy_followups[lane['key']] = [_compact(r) for r in ordered
            if r['source'] == 'previous' and not r.get('formal_recommendation')][:3]
        new_discoveries[lane['key']] = [_compact(r) for r in ordered
            if r['source'] != 'previous'][:3]
        strategy_front[lane['key']] = [_compact(r) for r in ordered if r['state'] in ACTIONABLE][:3]
    return {
        'selection_contract': (
            '正式推荐按正式priority；历史候选和新发现只按各自策略内的状态、正式排名、成交额和代码稳定排序；'
            '不同策略分数不跨策略比较；不得从混合表人工挑选。'
        ),
        'formal_recommendations': [_compact(r) for r in formal_rows],
        'strategy_followups': strategy_followups,
        'new_discoveries': new_discoveries,
        'strategy_front': strategy_front,
    }

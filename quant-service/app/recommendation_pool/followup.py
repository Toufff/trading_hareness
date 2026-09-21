"""Reuse the existing prospective ledger, retaining comparison non-selections."""
from ..effectiveness.manual import register
from ..strategy_origin import select_primary_origin


def register_decision(database, scan, bundle):
    groups = {}
    for pick in bundle['recommended']:
        primary = select_primary_origin(pick, stage=pick.get('stage'), context='recommendation_followup')
        groups.setdefault(primary['lane'], []).append(pick['symbol'])
    attribution = {p['symbol']:{'decision_id':bundle['decision_id'], 'editorial_position':i+1,
        'original_memberships':p.get('memberships',[]),
        'contemporaneous_reason':p.get('why_now') or p.get('selection_reason') or p.get('reason') or p.get('why_recommended'),
        'peer_comparison':p.get('comparison'),
        'ordering_basis':'editorial_not_cross_strategy_score'} for i,p in enumerate(bundle['recommended'])}
    return [register(database, scan, lane, symbols, 'formal_recommendation_pool',
                     'decision_id=' + bundle['decision_id'] + '; 条件触发前不是已成交；原始策略全量未选对照保留',
                     attribution=attribution)
            for lane, symbols in sorted(groups.items())]

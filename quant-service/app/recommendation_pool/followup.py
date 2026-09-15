"""Reuse the existing prospective ledger, retaining comparison non-selections."""
from ..effectiveness.manual import register


def register_decision(database, scan, bundle):
    groups = {}
    for pick in bundle['recommended']:
        membership = pick['memberships'][0] if pick['memberships'] else None
        if not membership:
            raise ValueError('recommended_stock_has_no_current_strategy_comparison')
        groups.setdefault(membership['lane'], []).append(pick['symbol'])
    return [register(database, scan, lane, symbols, 'formal_recommendation_pool',
                     'decision_id=' + bundle['decision_id'] + '; 条件触发前不是已成交；原始策略全量未选对照保留')
            for lane, symbols in sorted(groups.items())]

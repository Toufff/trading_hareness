"""Reuse the existing prospective ledger, retaining comparison non-selections."""
from ..effectiveness.manual import register
from ..strategy_origin import select_primary_origin


def register_decision(database, scan, bundle):
    groups = {}
    for pick in bundle['recommended']:
        primary = select_primary_origin(pick, stage=pick.get('stage'), context='recommendation_followup')
        groups.setdefault(primary['lane'], []).append(pick['symbol'])
    return [register(database, scan, lane, symbols, 'formal_recommendation_pool',
                     'decision_id=' + bundle['decision_id'] + '; 条件触发前不是已成交；原始策略全量未选对照保留')
            for lane, symbols in sorted(groups.items())]

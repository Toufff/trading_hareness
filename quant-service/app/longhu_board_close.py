"""Dated member aggregates, never today's board quote relabelled as history."""
from statistics import mean, median


def aggregate_board(identity, members, day):
    def numbers(key):
        return [float(r[key]) for r in members if isinstance(r.get(key), (int, float))]
    changes, ratios = numbers('pct_chg'), numbers('volume_ratio')
    amounts, flows = numbers('amount'), numbers('main_net')
    leaders = sorted(members, key=lambda r: float(r.get('main_net') or 0), reverse=True)[:10]
    return {
        'sector_key': identity['sector_key'], 'label': identity.get('label', identity['sector_key']),
        'taxonomy_key': 'longhu_ths_industry', 'trade_date': str(day),
        'mapped_members': len(members), 'quoted_members': len(members),
        'change_pct': mean(changes) if changes else None,
        'volume_ratio': median(ratios) if ratios else None,
        'amount': sum(amounts) if amounts else None,
        'net_inflow': sum(flows) if flows else None,
        'advancing_breadth': sum(v > 0 for v in changes) / len(changes) if changes else None,
        'top_stocks': [{'symbol': r['symbol'], 'name': r.get('name', r['symbol']),
                        'pct_change': r.get('pct_chg'), 'net_inflow': r.get('main_net')} for r in leaders],
        'source': 'longhuvip:dated_member_aggregate',
        'metric_basis': 'observed members: equal-weight return, summed amount/net, median volume ratio; NOT official board index',
    }

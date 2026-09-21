"""Canonical prices and cumulative factors on the immutable discovery basis."""
from ..research_prices import resolve_factors


def observation_prices(origin, sessions, bars, as_of_date):
    by = {str(b['trading_date']): dict(b) for b in bars}
    anchor = by.get(origin['signal_date'])
    if anchor is None and origin.get('adj_factor') is not None:
        anchor = {'trading_date': origin['signal_date'], 'close': origin['close'],
                  'adj_factor': origin['adj_factor']}
    days = sorted({str(d) for d in sessions if origin['signal_date'] < str(d) <= as_of_date})[:10]
    converted, flags, unresolved = [], set(), []
    prefix = [anchor] if anchor else []
    anchor_matches = bool(anchor and anchor.get('close') and origin.get('close') and
                          abs(float(anchor['close']) - float(origin['close'])) <= .011)
    for day in days:
        raw = by.get(day)
        if raw is None:
            # An explicit missing session prevents a later NULL factor being
            # carried across a sparse window. Real factors remain comparable.
            prefix.append({'trading_date': day})
            continue
        prefix.append(raw)
        resolution = resolve_factors(prefix) if anchor_matches else None
        usable = bool(resolution and resolution.usable)
        if resolution:
            flags.update(resolution.flags)
        if not usable:
            unresolved.append(day)
        scale = resolution.factors[-1] / resolution.factors[0] if usable else None
        row = dict(raw)
        for field in ('open','high','low','close','limit_up','limit_down'):
            row[field] = float(raw[field])*scale if scale is not None and raw.get(field) is not None else None
        converted.append(row)
    if not anchor_matches:
        flags.add('discovery_price_anchor_missing_or_mismatched')
    return converted, {'status': 'incomplete' if unresolved or not anchor_matches else 'ready',
                       'flags': sorted(flags), 'unresolved_dates': unresolved,
                       'basis': 'cumulative_factor_on_discovery_price_basis'}, by

"""Discovery survives execution restrictions. Neither is a trade authorization."""


def execution_context(metrics, route):
    flags = []
    # A diagnostic for the current non-ST main-board universe only. This is
    # NOT an exchange limit price or proof that the stock stayed sealed.
    if metrics['change_pct'] >= 9.7:
        flags.append('near_limit_up')
    if route['state'] in {'disabled', 'restricted'}:
        flags.append('market_restricted')
    return dict(status='wait_next_session', market_route=route['state'], flags=flags,
                buy_authorized=False,
                note=('接近主板涨停幅度，不证明封板或可成交；下一交易日重新核验开板承接、价格和风险。'
                      if 'near_limit_up' in flags else '观察资格不等于买入条件；下一交易日核验承接与风险。'))


def observation_list(rows, *, limit, per_industry):
    """Bound human-facing discovery without mixing risk multipliers into alpha.

    Crowded/broken/unverified structures remain explicitly labelled; inclusion
    cannot change the existing conditional shortlist or portfolio budgets.
    """
    counts, result = {}, []
    for row in sorted(rows, key=lambda r: (-r['discovery_score'], r['symbol'])):
        sector = row['sector_key']
        if counts.get(sector, 0) >= per_industry:
            continue
        result.append(row)
        counts[sector] = counts.get(sector, 0) + 1
        if len(result) >= limit:
            break
    return result

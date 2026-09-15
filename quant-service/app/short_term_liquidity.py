"""Shared research-priority evidence; never changes strategy matches or orders."""
from math import isfinite, log, sqrt
from statistics import median

VERSION = 'short-term-liquidity-2026-09-10'


def positive(value):
    try:
        v = float(value)
        return v if isfinite(v) and v > 0 else None
    except (ValueError, TypeError):
        return None


def evidence(amount, turnover, amounts=(), *, target=1_000_000_000):
    current = positive(amount)
    values = [v for a in list(amounts)[-5:] if (v := positive(a)) is not None]
    baseline = median(values) if len(values) >= 3 else None
    effective = sqrt(current * baseline) if current and baseline else (current or 0) * .5
    # Absolute activity saturates; it is not market-cap or expected-return rank.
    amount_score = max(0, min(100, log(max(effective, 50_000_000)/50_000_000)
                                 / log(max(target, 50_000_001)/50_000_000)*100))
    turn = positive(turnover) or 0
    turnover_score = min(100, turn/5*100) * (max(0, 1-(turn-25)/25) if turn > 25 else 1)
    return dict(version=VERSION, amount=current, median_amount_5d=baseline,
                effective_amount=effective, history_days=len(values), history_complete=len(values)==5,
                amount_score=round(amount_score,4), turnover_score=round(turnover_score,4),
                score=round(.75*amount_score+.25*turnover_score,4) if current else 0,
                scope='settled_daily' if baseline else 'current_only_discounted',
                note='成交额反映交易基础，不代表机构身份或后续上涨概率')


def rerank(rows, *, weight=.40, target=1_000_000_000):
    """Normalize only within the same strategy/risk state, then add liquidity.

    Ties have identical midrank; caution rows cannot perturb preferred ranks.
    Stored raw scores remain available; no cross-strategy score aggregation.
    """
    if not 0 <= weight <= 1:
        raise ValueError('liquidity weight must be in [0,1]')
    groups = {}
    for row in rows:
        groups.setdefault(row.get('state','watch'),[]).append(row)
    # Heat is separate evidence, not another name for adequate liquidity.
    # Keep new heat-ranking hypotheses shadow-only until temporal validation.
    effective_amounts = [evidence(r['metrics'].get('amount'), r['metrics'].get('turnover'),
        [b.get('amount') for b in r['metrics'].get('flow_series',[])], target=target)['effective_amount'] for r in rows]
    raw_values = [float(r['raw_score']) for r in rows]
    for group in groups.values():
        values = [float(r['raw_score']) for r in group]
        for row in group:
            raw = float(row['raw_score'])
            percentile = 100*(sum(v < raw for v in values)+.5*sum(v == raw for v in values))/len(values)
            m = row['metrics']
            liq = evidence(m.get('amount'),m.get('turnover'),
                           [r.get('amount') for r in m.get('flow_series',[])],target=target)
            row['liquidity'] = liq
            participation = 100*(sum(v < liq['effective_amount'] for v in effective_amounts)
                + .5*sum(v == liq['effective_amount'] for v in effective_amounts))/len(rows)
            row['attention'] = dict(participation_percentile=round(participation,4),
                amount_multiple=m.get('amount_multiple'), turnover=m.get('turnover'),
                sample_count=len(rows), scope='same_lane_matches', ranking_effect='shadow_only',
                note='成交参与度为同策略样本分位；相对放量与换手另列，不将大市值或放量下跌自动视为强势。')
            discovery_percentile = 100*(sum(v < raw for v in raw_values)
                + .5*sum(v == raw for v in raw_values))/len(rows)
            row['discovery_score'] = round((1-weight)*discovery_percentile+weight*liq['score'],4)
            row['ranking_components'] = dict(strategy_percentile=percentile, liquidity_weight=weight,
                                             liquidity_score=liq['score'], policy_version=VERSION)
            row['rank_score'] = round(((1-weight)*percentile+weight*liq['score'])
                                      *row.get('regime_route',{}).get('priority_weight',1),4)
    return rows

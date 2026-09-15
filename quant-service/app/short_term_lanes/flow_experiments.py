"""Read-only sensitivity panel; not a promoted model or return backtest.

Same settled input, no trades, no mutation, no automated promotion. Separates
removing the flow gate from changing the flow contribution in the ranking.
"""
from collections import defaultdict
from .accumulation_rules import evaluate


def compare(rows, sessions, settings, feature_fn, eligible_fn):
    grouped = defaultdict(list)
    for row in rows:
        if eligible_fn(row) and str(row['trade_date']).replace('-', '') <= sessions[-1].replace('-', ''):
            grouped[row['symbol']].append(row)
    variants = {key: [] for key in ('baseline', 'flow_gate_removed', 'flow_15pct', 'no_flow')}
    inspected = 0
    for symbol, bars in grouped.items():
        bars = sorted(bars, key=lambda r: str(r['trade_date']))
        f = feature_fn(bars, sessions)
        if not f or f['amount'] < settings.minimum_amount or f['turnover'] is None or not settings.minimum_turnover <= f['turnover'] <= settings.maximum_turnover:
            continue
        inspected += 1
        result = evaluate(sorted(bars, key=lambda r: str(r['trade_date'])))
        shape, flow = result['sideways']['score'], result['flow']['score']
        identity = dict(symbol=symbol, name=bars[-1].get('name'), flow_score=flow,
                        sideways_score=shape, amount=f['amount'], buy_authorized=False)
        if result['matched_intersection']:
            variants['baseline'].append(dict(identity, score=result['core_score']))
        if result['matched_sideways']:
            variants['flow_gate_removed'].append(dict(identity, score=result['core_score']))
            variants['flow_15pct'].append(dict(identity, score=round(.15*flow+.85*shape,4)))
            variants['no_flow'].append(dict(identity, score=shape))
    output = []
    for key, entries in variants.items():
        entries.sort(key=lambda r: (-r['score'], r['symbol']))
        output.append(dict(key=key, eligible_count=len(entries), top=entries[:10]))
    return dict(status='shadow_only', production_effect='none', scope='accumulation_only',
        as_of_date=sessions[-1], inspected=inspected, variants=output,
        note='仅比较资金门槛及分数敏感性；四组均要求已有活跃度和横盘条件。分数未叠加流动性、市场倍率或小盘因子，不能冒充正式名单；无资金数据的样本仍不进入本次配对对照。15%仅为实验假设，不是最优参数。尚无收益、回撤、费用、T+1与样本外验证，禁止自动启用。')

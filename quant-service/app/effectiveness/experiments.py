"""Registered ranking hypotheses; no best-of-many selection or live write path."""
from collections import defaultdict
from statistics import mean
from .rules import Policy, clean, daily_pairs, independent, numeric

VARIANTS={
    'attention':('成交参与优先','amount_multiple'),
    'recent_strength':('最近五日强度优先','return_5d'),
    'ma_penalty_half':('均线偏离惩罚减半','ma_gap_pct'),
    'flow_15pct':('资金15%与横盘85%','flow_score'),
}


def score(row,variant):
    f=row.get('features') or {}
    if variant=='flow_15pct':
        a,b=numeric(f.get('flow_score')),numeric(f.get('sideways_score'))
        return .15*a+.85*b if a is not None and b is not None else None
    if variant=='ma_penalty_half':
        raw,gap=numeric(f.get('structure_score')),numeric(f.get('ma_gap_pct'))
        return raw+max(0,gap)*.5 if raw is not None and gap is not None else None
    return numeric(f.get(VARIANTS[variant][1]))


def shadow_compare(rows, variant, policy=Policy(), *, holdout_start=None, registered_at=None, as_of='9999-12-31'):
    if variant not in VARIANTS:raise ValueError('Unregistered variant')
    keys={(r['lane'],r['profile'],r.get('regime'),r.get('source_kind')) for r in rows}
    if len(keys)>1:raise ValueError('Experiment cannot mix cohorts')
    valid=clean(rows,as_of)
    valid=[r for r in valid if r['timing']=='prospective']
    days=defaultdict(list)
    for r in valid:days[r['signal_date']].append(r)
    baseline=[];candidate=[];missing=0
    for day,rs in sorted(days.items()):
        if any(score(r,variant) is None for r in rs):missing+=1;continue
        baseline.extend(rs)
        for i,r in enumerate(sorted(rs,key=lambda r:(-score(r,variant),r['symbol'])),1):
            candidate.append({**r,'rank':i,'selected':i<=policy.top_n})
    a,_=daily_pairs(baseline,policy,as_of);b,_=daily_pairs(candidate,policy,as_of)
    by={p['date']:p for p in b};pairs=[{**p,'delta':by[p['date']]['top']-p['top']} for p in a if p['date'] in by]
    pairs=independent(pairs)
    split=max(1,len(pairs)//2)
    start=holdout_start or (pairs[split]['date'] if split<len(pairs) else '9999-12-31')
    train=[p for p in pairs if p['end_date']<start]
    # Embargo one full independent cohort around the train/test boundary.
    test=[p for p in pairs if p['date']>=start][1:]
    pre_registered=bool(registered_at and registered_at[:10]<start)
    enough=len(test)>=policy.minimum_sessions and sum(p['observations'] for p in test)>=policy.minimum_pairs
    delta=mean(p['delta'] for p in test) if test else None
    return dict(variant=variant,label=VARIANTS[variant][0],status='measured' if enough else 'insufficient',
        train_sessions=len(train),test_sessions=len(test),purged_sessions=len(pairs)-len(train)-len(test),
        missing_feature_dates=missing,holdout_start=start,pre_registered=pre_registered,
        test_delta_pp=delta,positive_date_fraction=mean(p['delta']>0 for p in test) if test else None,
        promotion_eligible=False,live_effect='none',
        notice='注册的单项排序对照，资格集合不变；未事前登记仅为探索，不可充当样本外通过；还需成交模拟与独立复核。')

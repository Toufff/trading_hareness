"""Paired, date-weighted diagnostics. Findings are hypotheses, not causal proof."""
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo
from math import isfinite
from statistics import mean

VERSION = 'effectiveness-feedback-2026-09-13'


@dataclass(frozen=True)
class Policy:
    horizon: int = 5
    top_n: int = 5
    minimum_sessions: int = 20
    minimum_pairs: int = 100
    coverage: float = .9
    gap_pp: float = -1.0
    losing_fraction: float = .7

    def __post_init__(self):
        if self.horizon not in (1,3,5,10) or self.top_n<1 or self.minimum_sessions<2 or self.minimum_pairs<2:
            raise ValueError('Invalid effectiveness policy')
        if not 0<self.coverage<=1 or not 0<self.losing_fraction<=1 or not isfinite(self.gap_pp):
            raise ValueError('Invalid diagnostic thresholds')


def numeric(v):
    try:
        n=float(v)
        return n if isfinite(n) else None
    except (TypeError,ValueError):return None


def cohort(r):
    return (r['lane'],r['profile'],r.get('regime','unknown'),r.get('source_kind','machine'),r.get('timing','reconstructed'))


def clean(rows, as_of):
    date.fromisoformat(as_of)
    grouped=defaultdict(list)
    for r in rows:
        if r['signal_date']>as_of:continue
        date.fromisoformat(r['signal_date'])
        # History is not promoted to prospective just because a caller relabels it.
        stamp=datetime.fromisoformat(r['available_at'])
        if stamp.tzinfo is None:raise ValueError('Timezone-aware available_at required')
        timing=r.get('timing','reconstructed')
        if stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()!=r['signal_date']:timing='reconstructed'
        row={**r,'timing':timing}
        grouped[(*cohort(row),row['signal_date'],row['symbol'])].append(row)
    result=[]
    for duplicates in grouped.values():
        # Earliest frozen decision wins, never select the best later revision.
        ordered=sorted(duplicates,key=lambda r:(datetime.fromisoformat(r['available_at']),r['origin_id']))
        result.append(ordered[0])
    return result


def daily_pairs(rows, policy, as_of):
    days=defaultdict(list)
    for r in rows:days[r['signal_date']].append(r)
    pairs=[];gaps=0
    for day,rs in sorted(days.items()):
        ordered=sorted(rs,key=lambda r:(r['rank'],r['symbol']))
        # Manual recommendations retain explicit selections, not a reordered TopN.
        if rs[0].get('source_kind')=='manual':
            top=[r for r in ordered if r.get('selected')];rest=[r for r in ordered if not r.get('selected')]
        else:top,rest=ordered[:policy.top_n],ordered[policy.top_n:]
        def eligible(r):
            w=r.get('window') or {}
            return w.get('status')=='observed' and r['signal_date']<str(w.get('date',''))<=as_of and numeric(w.get('return_pct')) is not None
        a=[r for r in top if eligible(r)];b=[r for r in rest if eligible(r)]
        if not top or not rest or len(a)!=len(top) or len(b)/len(rest)<policy.coverage:
            gaps+=1;continue
        pairs.append(dict(date=day,end_date=max(r['window']['date'] for r in a+b),
            top=mean(float(r['window']['return_pct']) for r in a),
            rest=mean(float(r['window']['return_pct']) for r in b),observations=len(a)+len(b),
            top_symbols=[r['symbol'] for r in a],rest_count=len(b)))
    return pairs,gaps


def independent(pairs):
    result=[];end=''
    for pair in sorted(pairs,key=lambda p:p['date']):
        if pair['date']>end:result.append(pair);end=pair['end_date']
    return result


def evaluate(rows, as_of, policy=Policy()):
    groups=defaultdict(list)
    for row in clean(rows,as_of):groups[cohort(row)].append(row)
    output=[]
    for key,rs in sorted(groups.items()):
        pairs,gaps=daily_pairs(rs,policy,as_of);sample=independent(pairs)
        n=len(sample);observations=sum(p['observations'] for p in sample)
        gap=mean(p['top']-p['rest'] for p in sample) if n else None
        loss=mean(p['top']<p['rest'] for p in sample) if n else None
        enough=n>=policy.minimum_sessions and observations>=policy.minimum_pairs
        status='exploratory' if key[4]!='prospective' else 'sufficient' if enough else 'insufficient'
        finding='top_underperforms' if status=='sufficient' and gap<=policy.gap_pp and loss>=policy.losing_fraction else None
        output.append(dict(lane=key[0],profile=key[1],regime=key[2],source_kind=key[3],timing=key[4],
            status=status,finding=finding,independent_sessions=n,paired_observations=observations,
            excluded_dates=gaps,overlapping_dates=len(pairs)-n,top_minus_rest_pp=gap,losing_fraction=loss,
            pairs=sample,minimum_sessions=policy.minimum_sessions,minimum_pairs=policy.minimum_pairs))
    return dict(version=VERSION,status='completed',as_of_date=as_of,policy=asdict(policy),groups=output,
        finding_count=sum(bool(g['finding']) for g in output),live_effect='none',
        notice='观察收益非成交收益；同策略/版本/市场/来源分组、日期等权并剔除重叠窗口。无异常不代表策略正确。')

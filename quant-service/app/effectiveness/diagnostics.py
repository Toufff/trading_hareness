"""Immediate descriptive coverage, deliberately not a profitability test."""
from collections import Counter


def describe(rows, groups):
    dates=sorted({r['signal_date'] for r in rows})
    observed=[r for r in rows if r.get('window',{}).get('status')=='observed']
    return {'layer':'descriptive_only','candidate_rows':len(rows),'distinct_signal_dates':len(dates),
        'first_signal_date':dates[0] if dates else None,'last_signal_date':dates[-1] if dates else None,
        'observed_windows':len(observed),
        'window_statuses':dict(Counter(r.get('window',{}).get('status','missing') for r in rows)),
        'semantic_profiles':len({r.get('profile') for r in rows}),
        'provenance_counts':dict(Counter(r.get('timing','unknown') for r in rows)),
        'groups':len(groups),'finding_groups':sum(bool(g.get('finding')) for g in groups),
        'effective_sample_is_not_candidate_count':True,'profitability_claim':False,
        'note':'覆盖与样本结构可立即检查；独立日期、成本及样本外门槛保持不变，未到期不记为失败。'}

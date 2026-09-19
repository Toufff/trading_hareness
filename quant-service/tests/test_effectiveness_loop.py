from copy import deepcopy
from datetime import date, timedelta

from app.effectiveness.rules import evaluate, Policy
from app.effectiveness.execution import simulate, Costs
from app.effectiveness.experiments import shadow_compare


def samples(days=25):
    rows=[]
    for d in range(days):
        day=date(2025,1,1)+timedelta(days=d*7)
        for i in range(10):
            rows.append(dict(origin_id=f'{d}-{i}',symbol=str(i),lane='trend',profile='p',regime='risk_off',
                source_kind='machine',timing='prospective',signal_date=str(day),available_at=str(day)+'T16:00:00+08:00',
                rank=i+1,display_rank=i+1 if i<5 else None,selected=i<5,
                window=dict(status='observed',date=str(day+timedelta(days=5)),return_pct=-2 if i<5 else 2),
                features=dict(return_5d=i,return_10d=i,ma_gap_pct=10-i,amount_multiple=i+1,
                              flow_score=100-i,sideways_score=i,liquidity_score=100)))
    return rows


def test_atomic_feedback_date_equal_weight_dedup_and_no_mutation():
    rows=samples();before=deepcopy(rows)
    result=evaluate(rows,'2026-09-13')
    assert result['groups'][0]['finding']=='top_underperforms'
    assert result['groups'][0]['independent_sessions']==25
    assert evaluate(rows+rows,'2026-09-13')['groups']==result['groups']
    assert rows==before and result['live_effect']=='none'


def test_history_version_missing_and_not_due_do_not_pass():
    rows=samples(2)
    assert evaluate(rows,'2026-09-13')['groups'][0]['status']=='insufficient'
    for r in rows:r['timing']='reconstructed'
    assert evaluate(rows,'2026-09-13')['groups'][0]['status']=='exploratory'
    for r in rows:r['profile']=r['origin_id']
    assert not any(g['finding'] for g in evaluate(rows,'2026-09-13')['groups'])
    rows=samples()
    for r in rows:
        if r['selected']:r['window']['status']='missing'
    assert not any(g['finding'] for g in evaluate(rows,'2026-09-13')['groups'])


def test_future_timing_and_overlapping_windows_not_independent():
    rows=samples()
    assert not evaluate(rows,'2024-01-01')['groups']
    for r in rows:r['window']['date']='2026-09-12'
    assert evaluate(rows,'2026-09-13')['groups'][0]['independent_sessions']==1
    rows=samples()
    for r in rows:r['available_at']='2026-09-13T12:00:00+08:00'
    assert not any(g['finding'] for g in evaluate(rows,'2026-09-13')['groups'])


def bars():
    return [dict(date='2026-09-14',open=10,high=10.5,low=9.5,close=10.2,limit_up=11,limit_down=9,adj_factor=1,is_suspended=False),
            dict(date='2026-09-15',open=10.2,high=10.8,low=10,close=10.5,limit_up=11.22,limit_down=9.18,adj_factor=1,is_suspended=False)]


def test_execution_costs_t1_missing_limits_and_adjustment():
    b=bars();r=simulate(b,['2026-09-14','2026-09-15'],Costs())
    assert r['status']=='simulated' and 0<r['net_return_pct']<5
    assert simulate(b[:1],['2026-09-14'],Costs())['status']=='t1_blocked'
    b[0]['limit_up']=None
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='missing_execution_data'
    b=bars();b[0]['open']=11
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='entry_limit_blocked'
    b=bars();b[1]['close']=b[1]['limit_down']
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='exit_limit_blocked'
    b=bars();b[1]['adj_factor']=2
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='corporate_action_unmodeled'


def test_a_not_yet_fetched_factor_is_pending_not_a_data_outage():
    # A NULL adj_factor means the separate factor lane has not run for that
    # date yet.  Reporting it as 'missing_execution_data' made a known-pending
    # control look like a bar outage; reporting it as 'corporate_action_unmodeled'
    # would claim a corporate action nobody observed.
    b=bars();b[1]['adj_factor']=None
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='adjustment_pending'
    b=bars();b[0]['adj_factor']=0
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='adjustment_pending'
    # Every other execution field is still mandatory, and two distinct real
    # factors are still an unmodelled corporate action, not a pending one.
    b=bars();b[1]['adj_factor']=None;b[1]['close']=None
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='missing_execution_data'
    b=bars();b[0]['adj_factor']=3;b[1]['adj_factor']=4
    assert simulate(b,['2026-09-14','2026-09-15'],Costs())['status']=='corporate_action_unmodeled'


def test_shadow_fixed_holdout_and_no_automatic_promotion():
    rows=samples(60);original=deepcopy(rows)
    result=shadow_compare(rows,'attention',Policy())
    assert result['live_effect']=='none' and result['promotion_eligible'] is False
    assert result['test_sessions']>0 and result['purged_sessions']>0
    assert result['variant']=='attention' and rows==original
    for r in rows:r['timing']='reconstructed'
    assert shadow_compare(rows,'attention',Policy())['status']=='insufficient'

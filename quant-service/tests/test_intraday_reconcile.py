from copy import deepcopy
import pytest
from app.intraday_scan.reconcile import compare

def fixture():
    item=dict(symbol='600000.SH',name='样本',lane='accumulation',state='platform_observation',price=10,reference=10.5,support=9.8,
              plan=dict(reference=10.5,support=9.8,state='armed',setup_kind='platform_hold',created_at='2026-09-14T14:52:00+08:00',expires_on='2026-09-14'))
    a=dict(cutoff='2026-09-14T14:50:00+08:00',observed_at='2026-09-14T14:52:00+08:00',input_hash='original',lanes=[dict(items=[item])])
    b=dict(cutoff='2026-09-14T15:00:00+08:00',observed_at='2026-09-14T15:10:00+08:00',
           rows=[dict(symbol='600000.SH',close=10.1,pct_chg=1,amount=5e8,main_net=1e7)],minutes={'600000.SH':[
               dict(time=t,close=10.1,vwap=10) for t in ('1450','1451','1452','1453','1454','1455','1500')]})
    c=dict(input_hash='closing',lanes=[dict(items=[{**item,'state':'confirmed_observation'}])])
    return a,b,c

def test_realistic_forward_plan_without_fill_or_source_mutation():
    a,b,c=fixture();original=deepcopy(a);r=compare(a,b,c)
    assert a==original and r['fills_verified']==0
    assert r['items'][0]['triggered_at']=='2026-09-14T14:55:00+08:00'
    assert r['items'][0]['observation_change_pct']==pytest.approx(1)

def test_late_source_not_backdated():
    a,b,c=fixture();a['observed_at']='2026-09-14T15:01:00+08:00'
    with pytest.raises(ValueError):compare(a,b,c)

def test_missing_quote_unknown_not_zero_return():
    a,b,c=fixture();b['rows']=[];r=compare(a,b,c)
    assert r['items'][0]['observation_change_pct'] is None
    assert r['items'][0]['minute_evidence'] is False

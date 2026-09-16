from copy import deepcopy
from unittest.mock import patch
import pytest
from app.intraday_scan import engine
from app.intraday_scan.rules import evaluate
from app.intraday_scan.repository import merge_formal_recommendations


def sample():
    rows = [dict(symbol=f'{600000+i}.SH', name='样本', close=10, pct_chg=0,
                 amount=4e8, main_net=1e7, turnover_rate=3, plate_id='test', trade_date='20260914') for i in range(3500)]
    return dict(cutoff='2026-09-14T11:30:00+08:00', observed_at='2026-09-14T11:33:00+08:00',
                history=[dict(symbol='600000.SH', trade_date='20260911')], sessions=['2026-09-11'],
                health=dict(plate_coverage=1), rows=rows, seeds=[], minutes={})


def core():
    return dict(status='completed', lanes=[dict(key=k, label=v, tracking_candidates=[], observation_list=[],
                total_matches=0, status='no_match', data_gaps={}) for k,v in engine.LABELS.items()])


def test_nine_same_input_no_mutation():
    d=sample(); original=deepcopy(d)
    with patch.object(engine,'screen',return_value=core()) as call:
        a=engine.build(d); b=engine.build(d)
    assert a==b and d==original and len(a['lanes'])==9
    assert call.call_args.args[0][-1]['main_net']==1e7
    assert a['daily_writes'] is False


def test_missing_ohlc_is_partial_not_full_market_no_match():
    c=core();lane=next(l for l in c['lanes'] if l['key']=='contraction')
    lane.update(status='completed',data_gaps={'strict_ohlc_40':419})
    with patch.object(engine,'screen',return_value=c):result=engine.build(sample())
    lane=next(l for l in result['lanes'] if l['key']=='contraction')
    assert lane['status']=='partial' and lane['total_matches']==0
    from app.intraday_scan.reports import render
    assert '未覆盖标的不视为不符合' in render(result)['contraction']


@pytest.mark.parametrize('kind',['wrong_date','duplicate','future_history','coverage','future_ohlc','retroactive_ohlc'])
def test_source_contract_failures(kind):
    d=sample()
    if kind=='wrong_date':d['rows'][0]['trade_date']='20260911'
    if kind=='duplicate':d['rows'][1]['symbol']=d['rows'][0]['symbol']
    if kind=='future_history':d['history'][0]['trade_date']='20260914'
    if kind=='coverage':d['health']['plate_coverage']=.3
    if kind in {'future_ohlc','retroactive_ohlc'}:
        d['price_histories']={'600000.SH':[dict(date='2026-09-14')]}
        d['ohlc_captured_at']='2026-09-14T15:10:00+08:00'
        if kind=='retroactive_ohlc':d['observed_at']='2026-09-14T15:20:00+08:00'
    with pytest.raises(ValueError):engine.validate(d)


def test_lunch_break_ohlc_provenance_matches_the_1130_cutoff():
    d=sample()
    d['observed_at']='2026-09-14T12:43:00+08:00'
    d['ohlc_captured_at']='2026-09-14T12:43:00+08:00'
    d['price_histories']={'600000.SH':[dict(date='2026-09-14')]}
    assert engine.validate(d).strftime('%H:%M')=='11:30'


def test_accumulation_inside_platform_not_breakout():
    d=sample(); d['seeds']=[dict(symbol='600000.SH',name='样本',lane='accumulation',source='previous',
        origin_id='x',reference=10.5,support=9.8)]
    d['minutes']={'600000.SH':[dict(time=t,close=10,vwap=9.99) for t in ('1128','1129','1130')]}
    with patch.object(engine,'screen',return_value=core()):r=engine.build(d)
    item=r['lanes'][0]['items'][0]
    assert item['state']=='platform_observation' and not item['buy_authorized']
    assert item['plan']['setup_kind']=='platform_hold'


def test_closed_never_arms_today_order():
    d=sample();d.update(cutoff='2026-09-14T15:00:00+08:00',observed_at='2026-09-14T15:30:00+08:00')
    d['seeds']=[dict(symbol='600000.SH',name='样本',lane='trend',source='previous',origin_id='x',reference=9.9,support=9.5)]
    d['minutes']={'600000.SH':[dict(time=t,close=10,vwap=9.99) for t in ('1458','1459','1500')]}
    with patch.object(engine,'screen',return_value=core()):r=engine.build(d)
    assert r['phase']=='after_close_initialization'
    assert next(l for l in r['lanes'] if l['key']=='trend')['items'][0]['plan']['state']=='after_close_watch_only'


def test_platform_future_trigger_is_not_fill():
    old=dict(reference=10.5,support=9.8,created_at='2026-09-14T11:27:00+08:00',expires_on='2026-09-14',state='armed',setup_kind='platform_hold')
    r=evaluate(dict(symbol='600000.SH',lane='accumulation',reference=10.5,support=9.8),sample()['rows'][0],
        [dict(time=t,close=10,vwap=9.99) for t in ('1128','1129','1130')],sample()['cutoff'],0,previous_plan=old)
    assert r['plan']['state']=='triggered_unverified_fill' and not r['buy_authorized']


def test_formal_recommendation_is_first_class_and_not_mixed_with_lane_rank():
    seeds = [dict(symbol='600000.SH', name='样本', lane='accumulation', source='previous',
                  origin_id='old', reference=10.5, support=9.8)]
    bundle = dict(decision_id='decision-1', as_of_date='2026-09-13', valid_until='2026-09-14T15:00:00+08:00',
                  recommended=[dict(symbol='600000.SH', name='正式第一', priority=1, stage='accumulation',
                                    data_date='2026-09-13', trigger='回踩确认', invalidation='跌破失效',
                                    memberships=[dict(lane='accumulation', rank=8)])])
    merged = merge_formal_recommendations(seeds, bundle)
    assert len(merged) == 1
    assert merged[0]['formal_recommendation'] is True
    assert merged[0]['recommendation_priority'] == 1
    assert merged[0]['recommendation_trigger'] == '回踩确认'


def test_presentation_is_deterministic_and_report_puts_formal_first():
    d=sample()
    d['seeds']=[dict(symbol='600000.SH',name='正式第一',lane='accumulation',source='previous',
        origin_id='formal',reference=10.5,support=9.8,formal_recommendation=True,
        recommendation_priority=1,recommendation_trigger='回踩确认',recommendation_invalidation='跌破失效'),
        dict(symbol='600001.SH',name='普通历史',lane='accumulation',source='previous',
        origin_id='history',reference=10.5,support=9.8,display_rank=1)]
    d['minutes']={s:[dict(time=t,close=10,vwap=9.99) for t in ('1128','1129','1130')]
                  for s in ('600000.SH','600001.SH')}
    with patch.object(engine,'screen',return_value=core()):result=engine.build(d)
    p=result['presentation']
    assert [r['symbol'] for r in p['formal_recommendations']]==['600000.SH']
    assert [r['symbol'] for r in p['strategy_followups']['accumulation']]==['600001.SH']
    from app.intraday_scan.reports import render
    report=render(result)['overview']
    assert report.index('昨日正式推荐跟踪') < report.index('各策略当前前排')
    assert '不得从混合表人工挑选' in report

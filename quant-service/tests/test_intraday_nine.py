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


def test_unmatched_old_candidate_is_not_reported_as_current_front():
    from app.intraday_scan.presentation import build as presentation
    old = dict(symbol='600001.SH', name='旧候选', lane='rotation', source='previous',
               state='platform_observation', matched_today=False, price=10, change_pct=1,
               amount=2e8, reference=10.5, support=9.5, reason='仍在观察区')
    lane = dict(key='rotation', label='轮动', items=[old], top=[old],
                total_matches=0, data_gaps={}, discovery_scope='测试', status='completed')
    projected = presentation([old], [lane])
    assert projected['strategy_front']['rotation'] == []
    assert [row['symbol'] for row in projected['strategy_followups']['rotation']] == ['600001.SH']
    assert projected['research_plan']['target_count'] == 0


def test_noon_research_floor_includes_each_current_lane_and_formal_carryover():
    from app.intraday_scan.presentation import build as presentation
    formal = dict(symbol='600000.SH', name='原推荐', lane='trend', source='previous',
                  formal_recommendation=True, recommendation_priority=1,
                  state='wait_confirmation', matched_today=False)
    first = dict(symbol='600001.SH', name='本轮首位', lane='trend', source='new_intraday',
                 state='confirmed_observation', matched_today=True, current_reason='本轮趋势转强')
    peer = dict(symbol='600002.SH', name='同组对照', lane='trend', source='new_intraday',
                state='wait_confirmation', matched_today=True)
    same = dict(**{**first, 'lane': 'reclaim'})
    lanes = [dict(key='trend', label='趋势', items=[first, peer, formal]),
             dict(key='reclaim', label='修复', items=[same])]
    p = presentation([formal, first, peer, same], lanes)
    assert p['research_plan']['target_count'] == 2
    targets = {row['symbol']: row for row in p['research_plan']['targets']}
    assert set(targets) == {'600000.SH', '600001.SH'}
    assert targets['600001.SH']['representative_lanes'] == ['trend', 'reclaim']
    assert targets['600001.SH']['comparison_peer']['symbol'] == '600002.SH'


def test_half_day_reason_does_not_treat_partial_amount_as_full_day_shrinkage():
    text = '5日净额+2.09亿元，成交额为前5日均值0.56倍'
    assert '0.56倍' not in engine._reason_at_cutoff(text, '2026-09-23T11:30:00+08:00')
    assert '待同刻基线' in engine._reason_at_cutoff(text, '2026-09-23T11:30:00+08:00')
    assert engine._reason_at_cutoff(text, '2026-09-23T15:00:00+08:00') == text


def test_intraday_reports_put_stock_conclusions_before_news_and_diagnostics():
    from app.intraday_scan.reports import render
    row = dict(symbol='600001.SH', name='本轮候选', lane='trend', source='new_intraday',
               state='confirmed_observation', matched_today=True, price=10, change_pct=1,
               amount=2e8, reference=10.5, support=9.5, reason='趋势修复',
               current_reason='本轮满足量价条件', original_reason='趋势修复',
               evidence_gaps=[], entry_scenario='放量站稳后观察')
    lane = dict(key='trend', label='趋势', items=[row], top=[row], total_matches=1,
                data_gaps={}, discovery_scope='测试', status='completed')
    result = dict(version='test', cutoff='2026-09-23T11:30:00+08:00', input_hash='hash',
                  observed_at='2026-09-23T11:35:00+08:00', history_through='2026-09-22',
                  market=dict(symbols=5000, up=2000, down=3000, median=-0.2), lanes=[lane],
                  event_research=dict(status='analyzed', summary='相关消息', events=[], leads=[]))
    reports = render(result)
    assert reports['overview'].index('本轮候选') < reports['overview'].index('消息变化与方向影响')
    assert reports['overview'].index('本轮候选') < reports['overview'].index('数据与边界')
    assert reports['trend'].index('本轮候选') < reports['trend'].index('消息变化与方向影响')
    assert '公司研究尚未由本报告完成' in reports['overview']

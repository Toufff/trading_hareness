from datetime import datetime, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo
import json
import asyncio
from unittest.mock import AsyncMock, patch
import pytest

from app.intraday_advisory.rules import QuoteSample, sample_from_row
from app.intraday_advisory.pressure import window_features, pressure_event
from app.intraday_advisory.renderer import signal_card, analysis_card
from app.intraday_advisory.delta import prepare_delta, bind_output
from app.intraday_advisory.pressure import feature_bundle
from app.agent_paper.model import ModelFailure, ModelResult

AT = datetime(2026, 9, 22, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai'))


def series(side=0.8, change=0.6, pulse=3):
    rows = []
    amount = volume = outer = inner = 0.0
    for i in range(109):
        rate = pulse if i > 96 else 1
        amount += 100000 * rate
        volume += 100 * rate
        outer += 100 * rate * side
        inner += 100 * rate * (1-side)
        rows.append(QuoteSample('600000.SH', AT-timedelta(seconds=(108-i)*5),
                                10 * (1 + max(0, i-96)/12*change/100), 10,
                                amount, volume, outer, inner, '浦发银行'))
    return rows


def test_windows_use_incremental_flow_amount_and_price():
    result = window_features(series(), 60)
    assert result['status'] == 'ready'
    assert abs(result['active_ratio'] - .6) < 1e-6
    assert result['amount_ratio'] == 3
    assert result['price_change_pct'] == .6
    assert result['pressure_state'] == 'buy_confirmed'
    assert result['amount_per_minute'] == 3600000
    assert result['price_impact_bps_per_10m'] > 0


def test_pressure_distinguishes_opposite_price_response():
    assert window_features(series(change=0), 60)['pressure_state'] == 'buy_stalled'
    assert window_features(series(side=.2, change=-.6), 60)['pressure_state'] == 'sell_confirmed'
    assert window_features(series(side=.2, change=0), 60)['pressure_state'] == 'sell_absorbed'
    assert window_features(series(side=.2, change=.6), 60)['pressure_state'] == 'divergent'


def test_missing_flow_is_not_zero_and_never_becomes_bullish():
    rows = [replace(x, outer_lot=None, inner_lot=None) for x in series()]
    result = window_features(rows, 60)
    assert result['active_ratio'] is None
    assert result['pressure_state'] == 'insufficient'


def test_gaps_resets_and_auction_fail_closed():
    rows = series()
    gap = window_features(rows[:-8]+rows[-1:], 60)
    assert gap['status'] == 'insufficient_window' and gap['reason'] == 'sample_gap'
    assert gap['gap_seconds'] == 40
    assert '采样中断' in gap['availability_note']
    reset = window_features(rows[:-1]+[replace(rows[-1], amount=1)], 60)
    assert reset['status'] == 'insufficient_window' and reset['reason'] == 'cumulative_reset'
    assert window_features([replace(x, observed_at=x.observed_at+timedelta(hours=5)) for x in rows], 60)['status'] != 'ready'
    bad = rows[:-1] + [replace(rows[-1], inner_lot=0)]
    assert window_features(bad, 60)['active_ratio'] is None


def test_opening_baseline_shortfall_is_distinct_from_a_quote_gap():
    rows = series()[-40:]
    feature = window_features(rows, 60)
    assert feature['status'] == 'ready'
    assert feature['baseline_reason'] == 'fewer_than_three_prior_windows'
    assert feature['volume_state'] == 'unknown'


def test_gap_covering_window_start_reports_interruption_not_generic_coverage():
    rows = [x for x in series() if not (AT-timedelta(minutes=3, seconds=23) < x.observed_at
                                         < AT-timedelta(minutes=1, seconds=37))]
    feature = window_features(rows, 180)
    assert feature['reason'] == 'sample_gap'
    assert feature['gap_seconds'] > 100
    assert '采样中断' in feature['availability_note']


def test_book_is_auxiliary_and_not_a_pressure_vote():
    rows = series(side=.2, change=-.6)
    plain = window_features(rows, 60)
    positive = window_features([replace(x, bid_depth=10000, ask_depth=1) for x in rows], 60)
    assert plain['pressure_state'] == positive['pressure_state'] == 'sell_confirmed'
    assert positive['book']['weibi_pct'] > 99
    assert positive['book']['used_in_pressure'] is False


def test_single_event_dedup_and_first_screen_has_direction_volume():
    rows = series(pulse=6)
    event = pressure_event(rows, None)
    assert event is not None
    assert pressure_event(rows, {'metrics':event.metrics}) is None
    card = signal_card(event, source='recommendation')
    first = json.dumps([x for x in card['body']['elements'] if x['tag'] != 'collapsible_panel'], ensure_ascii=False)
    assert '上涨' in first and '放量' in first and '偏多' in first
    assert '09:59' in first and '10:00' in first


def test_event_model_card_has_no_full_market_or_unrelated_sections():
    card = analysis_card('codex', {'headline':'承接条件发生变化', 'market_summary':'不应出现的全量大盘',
         'market_state':'watch', 'holding_focus':[], 'recommendation_focus':[],
         'delta_items':[{'symbol':'600000.SH','name':'浦发银行','scope':'recommendation',
                          'change':'偏空转为均衡','evidence':'近1分钟上涨0.2%，缩量',
                          'action':'等待原条件确认'}]}, report_kind='event', generated_at=AT)
    text = json.dumps(card, ensure_ascii=False)
    assert '不应出现的全量大盘' not in text
    assert '偏空转为均衡' in text
    assert '持仓 0' not in text


def payload():
    return {'as_of':AT.isoformat(),'report_kind':'event','market_context':{'unrelated':'market'},
            'recent_events':[{'symbol':'600000.SH','source':'holding'}],
            'scope':[{'symbol':'600000.SH','name':'浦发银行','scope':'holding','windows':feature_bundle(series()),
                      'position_or_recommendation':{'quantity':100,'business':'long unused dossier'},
                      'quote':{'price':10.06}}]}


def test_delta_prunes_unaffected_stocks_and_unchanged_notified_state():
    raw = payload()
    raw['scope'].append({**raw['scope'][0],'symbol':'600001.SH','name':'无关标的'})
    delta = prepare_delta(raw,{})
    assert len(delta['scope']) == 1 and delta['market_context'] == {}
    assert 'business' not in delta['scope'][0]['position_or_recommendation']
    assert prepare_delta(raw,{'600000.SH':raw['scope'][0]})['scope'] == []


def test_output_identity_and_action_number_fail_closed():
    data = prepare_delta(payload(),{})
    output = {'should_notify':True,'delta_items':[{'symbol':'600000.SH','name':'错误名称','action':'等待确认'}]}
    with pytest.raises(ModelFailure):
        bind_output(output,data)
    output['delta_items'][0]['name']='浦发银行'
    output['delta_items'][0]['action']='跌破9.99元卖出'
    with pytest.raises(ModelFailure):
        bind_output(output,data)
    output['delta_items'][0]['action']='等待原条件确认'
    result=bind_output(output,data)
    assert result['should_notify'] and '放量' in result['delta_items'][0]['evidence']
    assert bind_output({**output,'should_notify':False},data)['should_notify'] is False


def test_candidate_cannot_receive_sell_action():
    data=prepare_delta(payload(),{})
    data['scope'][0]['scope']='recommendation'
    with pytest.raises(ModelFailure):
        bind_output({'should_notify':True,'delta_items':[{'symbol':'600000.SH','name':'浦发银行','action':'立即减仓'}]},data)


def test_repository_baseline_is_sent_and_same_day_only():
    from app.intraday_advisory.repository import latest_delivered_context
    from unittest.mock import Mock
    connection=Mock()
    connection.execute.return_value.fetchall.return_value=[]
    assert latest_delivered_context(connection,at=AT)=={}
    sql=connection.execute.call_args.args[0]
    assert "d.status='sent'" in sql and 'Asia/Shanghai' in sql


def test_slow_model_does_not_block_next_quote_cycle():
    from app.intraday_advisory.runtime import RuntimeState, IntradayAdvisoryDependencies, run_intraday_advisory_cycle
    from app.intraday_advisory.scope import AdvisoryScope,ScopeItem
    async def run():
        state=RuntimeState(last_deepseek=AT,last_codex=None,pressure_day=AT.date())
        state.pending_events=[{'symbol':'600000.SH','source':'holding'}]
        state.pending_since=AT-timedelta(seconds=46)
        gate=asyncio.Event()
        async def slow(*args,**kwargs):
            await gate.wait()
            return {'status':'completed'}
        async def db(call): return call()
        deps=IntradayAdvisoryDependencies(database=object(),run_database=db,
            fetch_quotes=AsyncMock(return_value=[]),fetch_indices=AsyncMock(return_value={}),
            post_text=AsyncMock(),post_card=AsyncMock(),session_open=AsyncMock(return_value=(True,'open')),
            now=lambda:AT,account_key=lambda:'test')
        scope=AdvisoryScope('test',(ScopeItem('600000.SH','浦发银行','holding',{}),),None,None,())
        with patch('app.intraday_advisory.runtime._scope',return_value=scope), \
             patch('app.intraday_advisory.runtime._discipline',return_value=[]), \
             patch('app.intraday_advisory.runtime._latest_sector_snapshot',return_value=None), \
             patch('app.intraday_advisory.runtime._recent_quotes',return_value=[]), \
             patch('app.intraday_advisory.runtime._status'), \
             patch('app.intraday_advisory.runtime._analyze',side_effect=slow):
            await asyncio.wait_for(run_intraday_advisory_cycle(deps,state,now=AT),1)
            assert state.analysis_task is not None and not state.analysis_task.done()
            await asyncio.wait_for(run_intraday_advisory_cycle(deps,state,now=AT+timedelta(seconds=5)),1)
            assert deps.fetch_quotes.await_count==2
            gate.set()
            await state.analysis_task
    asyncio.run(run())


def test_minute_window_does_not_cross_lunch_or_day():
    rows=series()
    adjusted=[replace(x,observed_at=x.observed_at.replace(hour=11,minute=29)) for x in rows[-12:]]
    adjusted.append(replace(rows[-1],observed_at=AT.replace(hour=13,minute=0)))
    assert window_features(adjusted,60)['status']!='ready'


def test_weibi_requires_complete_five_levels_and_preserves_missing():
    raw={'ts_code':'600000.SH','price':10,'pre_close':10,
         'bids':[{'price':10,'size':100}]*4,'asks':[{'price':10.1,'size':10}]*5}
    sample=sample_from_row(raw,AT)
    assert sample.bid_depth is None and sample.ask_depth==50
    assert sample.outer_lot is None


def test_event_false_notify_cannot_be_overridden_by_always_push():
    from app.intraday_advisory.runtime import RuntimeState,IntradayAdvisoryDependencies,_analyze
    from app.intraday_advisory.scope import AdvisoryScope,ScopeItem
    async def run():
        state=RuntimeState()
        state.samples['600000.SH'].extend(series())
        state.pending_events=[{'symbol':'600000.SH','source':'holding'}]
        state.codex=AsyncMock()
        state.codex.analyze.return_value=ModelResult({'should_notify':False,'delta_items':[]},'mock',1,{})
        async def db(call):return call()
        deps=IntradayAdvisoryDependencies(database=object(),run_database=db,fetch_quotes=AsyncMock(),
            fetch_indices=AsyncMock(),post_text=AsyncMock(),post_card=AsyncMock(),session_open=AsyncMock(),
            now=lambda:AT,account_key=lambda:'test')
        scope=AdvisoryScope('test',(ScopeItem('600000.SH','浦发银行','holding',{}),),None,None,())
        with patch('app.intraday_advisory.runtime._delivered_context',return_value={}), \
             patch('app.intraday_advisory.runtime._persist_model_result',return_value={'analysis_run_id':'test'}) as saved, \
             patch('app.intraday_advisory.runtime._enqueue_analysis') as enqueue:
            result=await _analyze(deps,state,scope,provider='codex',trigger_kind='event',report_kind='event',always_push=True)
            assert result['status']=='completed' and result['pushed'] is False
            saved.assert_called_once()
            enqueue.assert_not_called()
    asyncio.run(run())


def test_neutral_noise_after_an_alert_is_not_a_new_event():
    original=pressure_event(series(pulse=6),None)
    quiet=series(side=.55,change=.05,pulse=1)
    assert pressure_event(quiet,{'metrics':original.metrics}) is None


def test_model_cannot_turn_60_seconds_into_60_minutes():
    data=prepare_delta(payload(),{})
    with pytest.raises(ModelFailure,match='model_repeated_computed_metric'):
        bind_output({'should_notify':True,'delta_items':[{'symbol':'600000.SH','name':'浦发银行',
                    'action':'60分钟窗口内等待确认'}]},data)


def test_longer_price_trigger_is_visible_not_hidden_by_flat_last_minute():
    rows=series(side=.55,change=0,pulse=1)
    # 3% climb in the earlier two minutes, then a flat minute.
    rows=[replace(x,price=10+(max(0,min(i-72,24)))/24*.3) for i,x in enumerate(rows)]
    event=pressure_event(rows,None)
    assert event and '近3分钟上涨' in event.summary and '近1分钟持平' in event.summary

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import replace
from unittest.mock import Mock
from unittest.mock import AsyncMock, patch
import asyncio
import json
from copy import deepcopy

from app.intraday_advisory.schedule import decide
from app.intraday_advisory.opening_guard import OpeningGuardVerdict, READY, FAILED, SKIPPED
from app.intraday_advisory.notice_policy import guard_notification, condition_signature, condition_changes

NOW = datetime(2026, 9, 22, 10, tzinfo=ZoneInfo('Asia/Shanghai'))


def test_only_three_briefing_windows_and_no_same_slot_deepseek():
    found = []
    for minute in range(9*60, 15*60+1):
        now = NOW.replace(hour=minute//60, minute=minute%60)
        result = decide(now, last_fetch=None, last_deepseek=None, last_codex=None)
        if result.run_codex:
            found.append((now.hour, now.minute, result.report_kind))
    assert found == [(10,0,'fixed'), (10,1,'fixed'), (11,35,'midday'), (11,36,'midday'),
                     (14,45,'tail'), (14,46,'tail')]
    result = decide(NOW+timedelta(seconds=20), last_fetch=NOW, last_deepseek=None, last_codex=NOW)
    assert not result.run_codex and not result.run_deepseek


def test_guard_success_silent_fault_dedup_and_recovery_only_if_announced():
    ready = OpeningGuardVerdict('live', READY, NOW, ({'name':'live_quote_flow','passed':True},))
    assert guard_notification(ready, {}) is None
    assert guard_notification(replace(ready, recovery_attempted=True), {}) is None
    failure = replace(ready, status=FAILED, recovery_attempted=True,
                      checks=({'name':'live_quote_flow','passed':False,'detail':'stale'},))
    assert guard_notification(failure, {}) == 'failure'
    state = {'open_fault': ['live_quote_flow']}
    assert guard_notification(failure, state) is None
    assert guard_notification(ready, state) == 'recovered'
    assert guard_notification(replace(ready, status=SKIPPED), state) is None
    assert guard_notification(replace(failure, recovery_attempted=False), {}) is None
    assert guard_notification(replace(ready,stage='preopen',checks=()),state) is None


def test_conditions_ignore_price_noise_but_capture_actual_plan_changes():
    item = {'scope':'holding', 'position_or_recommendation':{'stop_loss':9,'quantity':100},
            'quote':{'price':10}, 'windows':{'60':{'pressure_state':'sell_confirmed'}}}
    other = {**item,'quote':{'price':8},'windows':{}}
    assert condition_signature(item) == condition_signature(other)
    assert condition_changes(other,item) == []
    assert condition_changes(item,None) == []  # no invented prior plan
    other = {**item,'position_or_recommendation':{'stop_loss':9.5,'quantity':100}}
    assert condition_changes(other,item) == ['stop_loss']


def test_durable_brief_key_checks_active_delivery_not_just_model_completion():
    from app.intraday_advisory.repository import notification_exists
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None
    assert not notification_exists(connection, 'brief:2026-09-22:fixed')
    sql = connection.execute.call_args.args[0]
    assert "status='sent'" in sql and 'attempt_count<8' in sql


def test_discipline_precedence_is_scoped_to_same_holding_and_downside():
    from app.intraday_advisory.repository import signal_delivery_suppressed
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = {'covered':True}
    assert signal_delivery_suppressed(connection,'event',account_key='test')
    sql = connection.execute.call_args.args[0]
    assert "e.scope_source='holding'" in sql and "e.direction='down'" in sql
    assert "d.status='sent'" in sql and "payload->>'account_key'" in sql


def test_ten_minute_delta_requires_real_condition_change_not_new_quote():
    from app.intraday_advisory.delta import prepare_delta, bind_output
    item = {'symbol':'600000.SH','name':'浦发银行','scope':'holding',
            'quote':{'price':10,'observed_at':NOW.isoformat()},
            'windows':{'60':{'status':'ready'}},
            'position_or_recommendation':{'stop_loss':9,'quantity':100}}
    payload = {'scope':[item],'recent_events':[{'source':'market_index','summary':'指数下跌'}],
               'market_context':{},'as_of':NOW.isoformat(),'report_kind':'ten_minute'}
    assert prepare_delta(payload,{})['scope']==[]
    assert prepare_delta(payload,{item['symbol']:item})['scope']==[]
    before=deepcopy(item)
    before['position_or_recommendation']['stop_loss']=8
    delta=prepare_delta(payload,{item['symbol']:before})
    assert delta['scope'][0]['condition_changes']==['stop_loss']
    assert delta['market_events']==[]
    # Invalid/incomplete metrics stay explicitly unavailable, never fabricated.
    delta['scope'][0]['windows']={}
    bound=bind_output({'should_notify':True,'delta_items':[{
        'symbol':item['symbol'],'name':item['name'],'action':'按更新后的原条件核对'}]},delta)
    assert bound['delta_items'][0]['change']=='止损条件已更新'
    assert bound['delta_items'][0]['condition_evidence']=='止损条件：8 → 9'
    assert bound['should_notify']


def test_guard_cli_persists_only_successful_notifications(tmp_path):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    path=Path(__file__).resolve().parents[2]/'scripts/intraday-opening-guard.py'
    spec=importlib.util.spec_from_file_location('guard_cli_test',path)
    cli=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    state_path=tmp_path/'notice.json'
    args=SimpleNamespace(env_file=str(tmp_path/'test.env'),as_of=None,base_url='http://unused',
                         stage='live',recovery_attempted=True,notify=True,notification_state=str(state_path))
    failed=OpeningGuardVerdict('live',FAILED,NOW,({'name':'live_quote_flow','passed':False,'detail':'stale'},),True)
    ready=replace(failed,status=READY,checks=({'name':'live_quote_flow','passed':True,'detail':'fresh'},))
    sender=AsyncMock(return_value={'status':'sent'})
    with patch.object(cli,'_arguments',return_value=args), patch.object(cli,'load_dotenv'), \
         patch.object(cli,'Database'), patch.object(cli,'sse_calendar_status',return_value=(True,'open')), \
         patch.object(cli,'_get',return_value={}), patch.object(cli,'_notify',sender), \
         patch.object(cli,'evaluate_opening_guard',return_value=ready) as evaluate:
        assert cli.main()==0 and not state_path.exists()
        evaluate.return_value=failed
        assert cli.main()==2
        assert json.loads(state_path.read_text())['open_fault']==['live_quote_flow']
        assert cli.main()==2 and sender.await_count==1
        evaluate.return_value=ready
        assert cli.main()==0 and sender.await_count==2
        assert cli.main()==0 and sender.await_count==2


def test_failed_fault_notification_is_retryable(tmp_path):
    # Policy state is written only after a successful send (integration above).
    failed=OpeningGuardVerdict('live',FAILED,NOW,({'name':'api_health','passed':False},),True)
    assert guard_notification(failed,{})=='failure'
    assert guard_notification(failed,{})=='failure'
    expanded=replace(failed,checks=failed.checks+({'name':'discipline_tick','passed':False},))
    assert guard_notification(expanded,{'open_fault':['api_health']})=='failure'


def test_briefing_is_durable_across_runtime_restart_before_model_call():
    from app.intraday_advisory.runtime import _analyze, RuntimeState, IntradayAdvisoryDependencies
    from app.intraday_advisory.scope import AdvisoryScope
    async def run():
        async def db(call): return call()
        model=AsyncMock()
        deps=IntradayAdvisoryDependencies(database=object(),run_database=db,fetch_quotes=AsyncMock(),
              fetch_indices=AsyncMock(),post_text=AsyncMock(),post_card=AsyncMock(),session_open=AsyncMock(),
              now=lambda:NOW,account_key=lambda:'test',codex_factory=lambda:model)
        with patch('app.intraday_advisory.runtime._notice_exists',return_value=True) as exists:
            result=await _analyze(deps,RuntimeState(),AdvisoryScope('test',(),None,None,()),
                provider='codex',trigger_kind='scheduled',report_kind='fixed',always_push=True)
        assert result['reason']=='brief_already_queued'
        assert exists.call_args.args[1]=='brief:2026-09-22:fixed'
        model.analyze.assert_not_awaited()
    asyncio.run(run())


def test_delivery_discipline_precedence_does_not_touch_discipline_outbox():
    from app.intraday_advisory.runtime import _drain, IntradayAdvisoryDependencies
    async def run():
        async def db(call): return call()
        deps=IntradayAdvisoryDependencies(database=object(),run_database=db,fetch_quotes=AsyncMock(),
              fetch_indices=AsyncMock(),post_text=AsyncMock(return_value={'status':'sent'}),post_card=AsyncMock(),
              session_open=AsyncMock(),now=lambda:NOW,account_key=lambda:'test')
        with patch('app.intraday_advisory.runtime._load_due',return_value=[
               {'delivery_id':'covered','event_id':'e1','message_text':'重复下跌'},
               {'delivery_id':'different','event_id':'e2','message_text':'独立上涨'}]), \
             patch('app.intraday_advisory.runtime._discipline_covers',side_effect=[True,False]), \
             patch('app.intraday_advisory.runtime._delivery_outcome') as saved:
            result=await _drain(deps)
        assert result['sent']==1 and result['disabled']==1
        assert deps.post_text.await_args.args[0]=='独立上涨'
        assert saved.call_args_list[0].args[2]['reason']=='covered_by_discipline'
    asyncio.run(run())


def test_brief_risks_are_first_and_first_holding_is_not_account_action():
    from app.intraday_advisory.renderer import analysis_card
    card=analysis_card('codex',{'market_state':'risk','headline':'市场波动',
        'holding_focus':[{'symbol':'600000.SH','name':'浦发银行','action':'该股专属动作'}],
        'risks':['最高优先级风险']},report_kind='fixed',generated_at=NOW)
    elements=card['body']['elements']
    assert '最高优先级风险' in json.dumps(elements[0],ensure_ascii=False)
    action=next(x for x in elements if x.get('element_id')=='primary_action')
    assert '该股专属动作' not in json.dumps(action,ensure_ascii=False)
    assert card['header']['title']['content']=='早盘简报'
    assert 'Codex' not in card['header']['subtitle']['content']

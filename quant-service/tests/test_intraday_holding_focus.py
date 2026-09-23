from datetime import date, datetime, timedelta
import asyncio
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.intraday_advisory.focus import clear_focus, list_focus, session_expiry, set_focus, validate_identity
from app.intraday_advisory.focus_technicals import technical_evidence
from app.intraday_advisory.notice_policy import condition_changes
from app.intraday_advisory.delta import scope_signature
from app.intraday_advisory.renderer import signal_card
from app.intraday_advisory.rules import AdvisorySignal, QuoteSample
from app.trade_discipline.alerts_evaluation import ValidatedMinuteTape


TZ = ZoneInfo('Asia/Shanghai')


def test_focus_is_same_day_only_and_invalid_identity_is_rejected():
    at = datetime(2026, 9, 23, 11, 30, tzinfo=TZ)
    assert session_expiry(at) == at.replace(hour=15, minute=5)
    with pytest.raises(HTTPException) as ended:
        session_expiry(at.replace(hour=15, minute=6))
    assert ended.value.status_code == 409
    for account, symbol in [('bad space', '002315.SZ'), ('citics-primary', '002315;DROP')]:
        with pytest.raises(HTTPException):
            validate_identity(account, symbol)


def tape(closes, *, volumes=None, amounts=None):
    start = datetime(2026, 9, 23, 9, 31, tzinfo=TZ)
    rows = []
    volumes = volumes or [1000] * len(closes)
    amounts = amounts or [close * volume * 100 for close, volume in zip(closes, volumes)]
    for index, close in enumerate(closes):
        stamp = start + timedelta(minutes=index)
        rows.append({'time': stamp.strftime('%H%M'), 'close': close,
                     'volume_lot': volumes[index], 'amount': amounts[index],
                     'vwap': closes[0]})
    return ValidatedMinuteTape(date(2026, 9, 23), start + timedelta(minutes=len(rows)-1), tuple(rows))


def test_focus_technical_uses_completed_close_only_and_never_invents_ohlc():
    closes = [10 + (.02 if index % 2 else -.02) for index in range(60)]
    closes += [10 + index * .035 for index in range(1, 8)]
    amounts = [1_000_000] * 60 + [2_000_000] * 7
    result = technical_evidence(tape(closes, amounts=amounts))
    assert result['status'] == 'ready'
    assert result['state'] == 'bullish_confirmation'
    assert result['macdfs'] > 0 and result['macdfs_rising_3m']
    assert result['change_3m_pct'] > .3 and result['amount_ratio_3m'] >= 1.3
    assert result['unsupported'] == ['minute_kdj', 'minute_atr', 'minute_adx']
    assert result['macdfs_formula'].startswith('2*')
    assert 'high' not in result and 'low' not in result


def test_flat_low_volume_is_sideways_not_buy_point():
    closes = [10.0] * 65
    amounts = [1_000_000] * 62 + [400_000] * 3
    result = technical_evidence(tape(closes, amounts=amounts))
    assert result['state'] == 'sideways'
    assert result['decision_boundary'] == 'observation_only_not_bs_or_order'


def test_incomplete_or_invalid_minute_data_fails_closed():
    assert technical_evidence(tape([10] * 20))['status'] == 'insufficient_history'
    bad = tape([10] * 65)
    bad.rows[-1]['amount'] = None
    assert technical_evidence(bad)['state'] in {'mixed', 'sideways'}
    bad.rows[-1]['close'] = None
    assert technical_evidence(bad)['status'] == 'invalid_tape'


def test_focus_state_change_is_material_but_missing_feed_is_not_market_reversal():
    fact = {'monitoring_focus': {'intent': 'intraday_t'}}
    mixed = {'position_or_recommendation': fact, 'focus_technicals': {'status': 'ready', 'state': 'mixed'}}
    bullish = {'position_or_recommendation': fact,
               'focus_technicals': {'status': 'ready', 'state': 'bullish_confirmation'}}
    missing = {'position_or_recommendation': fact, 'focus_technicals': {'status': 'unavailable'}}
    assert condition_changes(bullish, mixed) == ['technical_state']
    assert scope_signature(bullish) != scope_signature(mixed)
    assert condition_changes(missing, bullish) == []
    assert condition_changes(mixed, {'position_or_recommendation': fact}) == []


def test_focused_pressure_card_adds_causal_evidence_without_bs_claim():
    from app.intraday_advisory.pressure import pressure_event
    end = datetime(2026, 9, 23, 10, 0, tzinfo=TZ)
    rows = [QuoteSample('002315.SZ', end-timedelta(seconds=(108-i)*5),
                        10*(1+max(0,i-96)/12*.6/100), 10,
                        i*100_000 if i<=96 else 96*100_000+(i-96)*600_000,
                        i*100 if i<=96 else 96*100+(i-96)*600,
                        i*80 if i<=96 else 96*80+(i-96)*480,
                        i*20 if i<=96 else 96*20+(i-96)*120, '焦点科技') for i in range(109)]
    pressure = pressure_event(rows, None)
    assert pressure is not None
    technical = technical_evidence(tape([10.0] * 65, amounts=[1_000_000] * 62 + [400_000] * 3))
    signal = AdvisorySignal(pressure.event_key, pressure.symbol, pressure.name, pressure.kind,
                            pressure.direction, pressure.severity, pressure.observed_at,
                            {**pressure.metrics, 'focus_technicals': technical}, pressure.summary)
    import json
    text = json.dumps(signal_card(signal, source='holding'), ensure_ascii=False)
    assert '今日重点监控' in text and '标准分时 MACD 柱' in text
    assert '不是独立 B/S 点' in text and '真实分钟最高' not in text


def test_manual_focus_is_readable_mutable_and_restricted_to_verified_holdings():
    class Result:
        def __init__(self, one=None, many=None): self.one, self.many = one, many or []
        async def fetchone(self): return self.one
        async def fetchall(self): return self.many

    class Connection:
        focused = False
        async def execute(self, sql, params=()):
            if 'FROM quant.broker_portfolio_snapshots' in sql:
                return Result({'snapshot_id': 'snap', 'observed_at': datetime(2026, 9, 22, 15, tzinfo=TZ),
                               'verification': 'verified_exact', 'metadata': {}})
            if 'LEFT JOIN quant.intraday_holding_focus' in sql:
                return Result(many=[{'symbol': '002315.SZ', 'name': '焦点科技', 'quantity': 800,
                                     'intent': 'intraday_t' if self.focused else None,
                                     'expires_at': session_expiry(NOW) if self.focused else None}])
            if 'SELECT name FROM quant.broker_position_snapshots' in sql:
                return Result({'name': '焦点科技'} if params[-1] == '002315.SZ' else None)
            if 'INSERT INTO quant.intraday_holding_focus' in sql:
                self.focused = True
            if 'DELETE FROM quant.intraday_holding_focus' in sql:
                self.focused = False
            return Result()

    class Database:
        connection = Connection()
        def transaction(self): return self
        async def __aenter__(self): return self.connection
        async def __aexit__(self, *_): return False

    async def scenario():
        db = Database()
        assert not (await list_focus(db, 'citics-primary', NOW))['items'][0]['focused']
        with pytest.raises(HTTPException) as not_held:
            await set_focus(db, 'citics-primary', '600000.SH', NOW)
        assert not_held.value.status_code == 409
        applied = await set_focus(db, 'citics-primary', '002315.SZ', NOW)
        assert applied['focused'] and applied['expires_at'] == session_expiry(NOW)
        assert (await list_focus(db, 'citics-primary', NOW))['items'][0]['focused']
        await clear_focus(db, 'citics-primary', '002315.SZ')
        assert not (await list_focus(db, 'citics-primary', NOW))['items'][0]['focused']
    asyncio.run(scenario())


def test_slow_focus_minute_tape_cannot_block_five_second_quote_cycle():
    from app.intraday_advisory.runtime import IntradayAdvisoryDependencies, RuntimeState, run_intraday_advisory_cycle
    from app.intraday_advisory.scope import AdvisoryScope, ScopeItem
    gate = asyncio.Event()

    async def slow_minutes(_symbol):
        await gate.wait()
        return {'session_date': '2026-09-23', 'rows': []}

    async def run_database(call): return call()

    async def scenario():
        at = NOW.replace(hour=10, minute=0)
        deps = IntradayAdvisoryDependencies(database=object(), run_database=run_database,
            fetch_quotes=AsyncMock(return_value=[]), fetch_indices=AsyncMock(return_value={}),
            post_text=AsyncMock(), post_card=AsyncMock(), session_open=AsyncMock(return_value=(True, 'open')),
            now=lambda: at, account_key=lambda: 'citics-primary', fetch_minutes=slow_minutes)
        scope = AdvisoryScope('citics-primary', (
            ScopeItem('002315.SZ', '焦点科技', 'holding', {'monitoring_focus': {'intent': 'intraday_t'}}),
        ), 'snap', None, ())
        state = RuntimeState(pressure_day=at.date(), last_codex=at, last_deepseek=at)
        with patch('app.intraday_advisory.runtime._scope', return_value=scope), \
             patch('app.intraday_advisory.runtime._discipline', return_value=[]), \
             patch('app.intraday_advisory.runtime._latest_sector_snapshot', return_value=None), \
             patch('app.intraday_advisory.runtime._status'):
            await asyncio.wait_for(run_intraday_advisory_cycle(deps, state, now=at), 1)
            assert state.focus_task is not None and not state.focus_task.done()
            await asyncio.wait_for(run_intraday_advisory_cycle(deps, state, now=at+timedelta(seconds=5)), 1)
            assert deps.fetch_quotes.await_count == 2
            gate.set()
            await state.focus_task
    asyncio.run(scenario())


NOW = datetime(2026, 9, 23, 11, 30, tzinfo=TZ)

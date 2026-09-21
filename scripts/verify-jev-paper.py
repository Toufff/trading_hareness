"""Real JEV calls + rollback-only paper ledger acceptance; never a broker call.

Historical model inputs are frozen evidence, not a backtest. Model calls happen
before opening the rollback ledger transaction. Forced buy/sell cases are
separately labelled plumbing tests, never represented as JEV decisions.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv
from app.database import Database
from app.agent_paper import repository as repo
from app.agent_paper.context import build_context, compact_quote
from app.agent_paper.jev import JevPaperModel, MODEL, build_request, decode_choice
from app.agent_paper.model import ModelFailure, ModelResult
from app.agent_paper.report import status
from app.agent_paper.rules import dec, fees_for
from app.agent_paper.runner import Runner, run_day
from app.longhu_vendor_source import parse_stock_snapshot_payload

SH = ZoneInfo('Asia/Shanghai')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))


def replay_quotes(context):
    result = {}
    for symbol, detail in context.get('detail_symbols', {}).items():
        q = detail.get('quote') or {}
        if not q:
            continue
        result[symbol] = {'ts_code': symbol, 'name': q.get('name'), 'price': q.get('price'),
                          'trade_time': q.get('quote_time'),
                          'bids': [{'price': p, 'size': n} for p, n in q.get('bids', [])],
                          'asks': [{'price': p, 'size': n} for p, n in q.get('asks', [])]}
    return result


class RecordedResult:
    def __init__(self, result, context):
        self.result, self.context, self.model = result, context, result.model

    def decide(self, context_json):
        assert json.loads(context_json) == json.loads(encoded(self.context)), 'replay context changed'
        return self.result


async def ledger_check(db, context, result, *, loop=False):
    key = 'jev-acceptance-' + uuid4().hex
    now = datetime.fromisoformat(context['now']).replace(tzinfo=SH)
    quotes = replay_quotes(context)
    baseline = {'cash': context['account']['cash'], 'positions': [
        {'symbol': p['symbol'], 'name': p.get('name'), 'quantity': p['quantity'],
         'sellable_quantity': p.get('sellable', 0), 'average_cost': p.get('avg_cost', 0)}
        for p in context['account'].get('positions', [])]}
    baseline['source_account'] = 'jev-acceptance-no-human-comparison'
    baseline['start_at'] = now.isoformat()
    evidence = {}
    with db.transaction() as connection:
        # Even exceptions cannot commit these rows. No HTTP call occurs inside.
        with connection.transaction(force_rollback=True):
            repo.create_account(connection, account_key=key, model=result.model, start_date=now.date(),
                                baseline=baseline, initial_equity=dec(context['account'].get('equity_live', 100000)))

            class RollbackDatabase:
                @contextmanager
                def transaction(self):
                    with connection.transaction():
                        yield connection

            async def frozen_context(factory, **kwargs):
                return copy.deepcopy(context), copy.deepcopy(quotes)

            async def frozen_quotes(symbols):
                return {s: copy.deepcopy(quotes[s]) for s in symbols if s in quotes}

            current = now
            runner = Runner(RollbackDatabase(), key, RecordedResult(result, context),
                            fetch_quotes=frozen_quotes, context_builder=frozen_context,
                            clock=lambda: current, decision_minutes=15)
            if loop:
                points = iter([now + timedelta(seconds=60), datetime.combine(now.date(), time(15, 1), SH)])

                async def advance(seconds):
                    nonlocal current
                    current = next(points, datetime.combine(now.date(), time(15, 2), SH))

                events = []
                summary = await run_day(runner, now_fn=lambda: current, sleep=advance, log=events.append)
                assert summary['decisions'] == 1, summary
                assert not any(e['event'] in {'pass_failed', 'decisions_halted'} for e in events), events
                evidence['loop'] = {'decisions': summary['decisions'], 'events': [e['event'] for e in events],
                                    'nav_recorded': summary['recorded']}
                outcome = next(e for e in events if e['event'] == 'decision')
            else:
                outcome = await runner.decide(now)
                projected = status(connection, account_key=key, day=now.date())
                assert projected['decisions_today'] == {'decided': 1, 'failed': 0}, projected
            assert outcome['status'] == 'decided', outcome
            row = connection.execute('SELECT context,transcript,output,outcomes FROM quant.agent_paper_decisions '
                                     'WHERE decision_id=%s', (outcome['decision_id'],)).fetchone()
            assert row['context'] == json.loads(encoded(context))
            assert row['transcript'] == json.loads(encoded(result.transcript))
            assert row['output'] == json.loads(encoded(result.output))
            account = repo.load_account(connection, key)
            positions = repo.load_positions(connection, key)
            assert dec(account['cash']) >= 0
            assert all(0 <= p['sellable_quantity'] <= p['quantity'] for p in positions)
            evidence.update(outcome=outcome, cash=account['cash'], positions=positions,
                            decision_and_transcript_readback=True)
    with db.transaction() as connection:
        assert repo.load_account(connection, key) is None, 'rollback did not remove scratch account'
        assert connection.execute('SELECT count(*) n FROM quant.agent_paper_decisions WHERE account_key=%s',
                                  (key,)).fetchone()['n'] == 0
    evidence['rollback_verified'] = True
    return evidence


def plumbing_case(day, side):
    # Numeric morning HHMMSSmmm is intentionally not padded by the fixture:
    # this exercises the provider parser that previously created 93:00:30.
    snapshot = parse_stock_snapshot_payload({
        'code': '000811', 'name': '冰轮环境', 'day': day.strftime('%Y%m%d'), 'preclose_px': 10,
        'real': {'last_px': 10, 'time': 93003000},
        'weituo': {'s1': [10.01, 100], 'b1': [9.99, 100]},
    }, '000811.SZ')
    assert snapshot['trade_time'] == day.strftime('%Y%m%d') + '093003'
    context = {'now': f'{day} 09:30:03', 'account': {'cash': 10000, 'equity_live': 20000,
               'positions': [{'symbol': '000811.SZ', 'name': '冰轮环境', 'quantity': 1000,
                              'sellable': 1000, 'avg_cost': 10}], 'open_orders': []},
               'detail_symbols': {'000811.SZ': {'quote': compact_quote(snapshot)}}}
    _, options = build_request(context, MODEL)
    choice = next(k for k, v in options.items() if v and v['action'] == side)
    payload = {'answers': {'action': {'type': 'choice', 'choice': choice, 'confidence': 1,
               'probabilities': {k: float(k == choice) for k in options}}}}
    result = ModelResult(output=decode_choice(payload, options), model='jev-fixture-NOT-MODEL-DECISION',
                         duration_ms=0, transcript=[{'type': 'forced_plumbing_fixture', 'response': payload}])
    return context, result


async def verify(args, db, report):
    model = JevPaperModel()
    with db.transaction() as connection:
        account = repo.load_account(connection, args.account_key)
        if account is None:
            raise ValueError('pilot account is not initialized')
        positions = repo.load_positions(connection, args.account_key)
        before = status(connection, account_key=args.account_key)
        samples = [dict(r) for r in connection.execute(
            'SELECT decision_id,account_key,decided_at,context FROM quant.agent_paper_decisions '
            'WHERE trading_date=%s AND context IS NOT NULL ORDER BY decided_at', (args.day,)).fetchall()]
    assert samples, 'no intraday contexts on requested day'
    ready = []
    for sample in samples:
        request, options = build_request(sample['context'], model.model)
        sample['request_bytes'] = len(json.dumps(request, ensure_ascii=False).encode('utf-8'))
        sample['options'] = len(options)
        ready.append(sample)
    report['sample_audit'] = {'count': len(ready), 'max_request_bytes': max(s['request_bytes'] for s in ready),
                              'over_budget': sum(s['request_bytes'] > 90000 for s in ready),
                              'wait_only': sum(s['options'] == 1 for s in ready)}
    assert not report['sample_audit']['over_budget'], report['sample_audit']
    chosen = [next(s for s in ready if s['options'] > 1), max(ready, key=lambda s:s['request_bytes']),
              next(s for s in ready if s['decided_at'].astimezone(SH).hour >= 13), ready[-1]]
    chosen = list({str(s['decision_id']): s for s in chosen}.values())
    if args.all_stored:
        chosen = ready
    for sample in chosen:
        result = await asyncio.to_thread(model.decide, encoded(sample['context']))
        ledger = await ledger_check(db, sample['context'], result,
                                    loop=sample is chosen[-1])
        case = {'source_id': str(sample['decision_id']), 'source_account': sample['account_key'],
                'at': str(sample['decided_at']), 'duration_ms': result.duration_ms, 'usage': result.usage,
                'output': result.output, 'transcript': result.transcript, 'ledger': ledger}
        report['cases'].append(case)
        print(encoded({'event': 'historical_case_passed', 'at': case['at'], 'duration_ms': result.duration_ms,
                       'orders': ledger['outcome']['orders']}), flush=True)
    report['forced_plumbing'] = {}
    for side in ['buy', 'sell']:
        context, result = plumbing_case(args.day, side)
        ledger = await ledger_check(db, context, result)
        fill = ledger['outcome']['orders'][0]
        assert fill['status'] == 'filled', fill
        order = result.output['orders'][0]
        gross = order['quantity'] * order['limit_price']
        cost = fees_for(side, order['quantity'], order['limit_price'])
        assert ledger['cash'] == Decimal(10000) + (-gross if side == 'buy' else gross) - cost
        assert ledger['positions'][0]['sellable_quantity'] == (1000 if side == 'buy' else 1000-order['quantity'])
        report['forced_plumbing'][side] = ledger
    now = datetime.now(SH)
    live_context, quotes = await build_context(db.transaction, now=now, account=account, positions=positions,
                                              open_orders=[], today_orders=[], recent_decisions=[])
    live = await asyncio.to_thread(model.decide, encoded(live_context))
    assert not live.output['orders'], 'after-hours should never offer orders'
    live_ledger = await ledger_check(db, live_context, live)
    report['current_provider_read'] = {
        'checked_at': now.isoformat(), 'quotes': len(quotes), 'indices': len(live_context.get('indices', {})),
        'detail_symbols': len(live_context.get('detail_symbols', {})),
        'held_quote_coverage': sum(p['symbol'] in quotes for p in positions), 'held_count': len(positions),
        'quote_times': {p['symbol']: (quotes.get(p['symbol']) or {}).get('trade_time') for p in positions},
        'duration_ms': live.duration_ms, 'usage': live.usage, 'ledger': live_ledger,
        'transcript': live.transcript, 'note': 'after-hours quotes, not live intraday freshness acceptance'}
    assert report['current_provider_read']['held_quote_coverage'] == len(positions), 'held quote coverage incomplete'
    assert report['current_provider_read']['indices'] >= 4, 'index reads incomplete'
    with db.transaction() as connection:
        after = status(connection, account_key=args.account_key)
    assert before == after, 'pilot ledger unexpectedly changed'
    report['passed'] = True
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--day', type=date.fromisoformat, default=datetime.now(SH).date())
    p.add_argument('--account-key', default='agent-jev-pilot')
    p.add_argument('--all-stored', action='store_true', help='Call JEV for every stored context on the requested day')
    p.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--provider-env-file', default='G:/StockPlatform/config/jev-paper.env')
    p.add_argument('--out-dir', type=Path, default=Path('G:/StockPlatform/reports/jev-pilot'))
    args = p.parse_args()
    # This command deliberately cannot be mistaken for an intraday launch.
    from app.agent_paper.runner import in_session
    if in_session(datetime.now(SH)):
        raise SystemExit('Run this rollback replay verifier outside trading hours')
    load_dotenv(args.env_file, override=True)
    load_dotenv(args.provider_env_file, override=True)
    db = Database()
    report = {'passed': False, 'mode': 'after_hours_readiness_and_replay', 'realtime_session_verified': False,
              'live_orders': False, 'paper_account_changed': False, 'day': str(args.day), 'cases': []}
    try:
        asyncio.run(verify(args, db, report))
    except ModelFailure as error:
        report.update(passed=False, error=error.code, detail=error.detail, failed_transcript=error.transcript)
    except Exception as error:
        # Retain completed cases even when a later source or ledger check fails.
        report.update(passed=False, error=type(error).__name__, detail=str(error)[:500])
    finally:
        db.close()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / (datetime.now(SH).strftime('%Y%m%dT%H%M%S')+'-readiness.json')
    path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
    print(encoded({'passed': report['passed'], 'artifact': str(path), 'sample_audit': report.get('sample_audit'),
                   'error': report.get('error'), 'realtime_session_verified': False}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

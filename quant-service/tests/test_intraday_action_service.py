import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch
import tempfile
from pathlib import Path
from contextlib import ExitStack, closing

from app.intraday_actions.repository import ShadowRepository
from app.intraday_actions.service import ActionShadowService, assemble_contexts


def context():
    return {'now': '2026-09-23T02:00:00+00:00', 'account_key': 'a', 'symbol': 's',
            'plan': {'id': 'p', 'version': '1', 'expires_at': '2026-09-23T07:00:00+00:00'},
            'quote': {'as_of': '2026-09-23T02:00:00+00:00'},
            'policy': {'max_quote_age_seconds': 20}, 'account': {}}


def evaluator(c):
    event = {'event_key': c['symbol'], 'action': 'first_buy', 'quantity': 100,
             'price_range': [10, 11], 'plan_id': 'p', 'plan_version': '1'}
    return {'status': 'ready', 'states': {'buy': 'consumed'}, 'coverage': [{'action': 'first_buy', 'state': 'confirmed'}], 'blockers': [],
            'events': [event], 'confirmed_candidates': [event]}


class ActionServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = ShadowRepository(':memory:')
        self.service = ActionShadowService(self.repo, evaluator=evaluator)

    def test_reserves_cash_account_wide_and_restart_dedup(self):
        seen = []
        def check(c):
            seen.append(c['account']['allocated_buy_cash'])
            return evaluator(c)
        self.service.evaluator = check
        first = context()
        first['account']['costs'] = {'commission_rate': .0002, 'min_commission': 5, 'transfer_rate': .00001, 'slippage_bps': 2}
        second = context()
        second['symbol'] = 'other'
        self.service.evaluate_batch([first, second])
        self.assertEqual(seen[0], 0)
        self.assertGreater(seen[1], 1105)
        self.service.evaluate_batch([first])
        self.assertEqual(len(self.repo.pending()), 2)

    def test_expired_delivery_never_calls_transport(self):
        self.service.evaluate_batch([context()])
        result = self.service.dispatch(now=datetime(2026, 9, 23, 2, 1, tzinfo=timezone.utc),
            current_context=lambda event: context(), send=lambda event: self.fail('must not send'))
        self.assertEqual(result[0]['status'], 'expired')

    def test_plan_revised_before_send_invalidates(self):
        self.service.evaluate_batch([context()])
        result = self.service.dispatch(now=datetime(2026, 9, 23, 2, 0, 1, tzinfo=timezone.utc),
            current_context=lambda event: {**context(), 'plan': {'id': 'p', 'version': '2'}}, send=lambda event: self.fail('must not send'))
        self.assertEqual(result[0]['status'], 'invalidated')

    def test_definite_rejection_retries_but_unknown_outcome_is_not_retried(self):
        self.service.evaluate_batch([context()])
        args = {'now': datetime(2026, 9, 23, 2, 0, 1, tzinfo=timezone.utc),
                'current_context': lambda event: context()}
        self.assertEqual(self.service.dispatch(**args, send=lambda event: False)[0]['status'], 'pending')
        def timeout(event):
            raise TimeoutError('response was lost')
        self.assertEqual(self.service.dispatch(**args, send=timeout)[0]['status'], 'uncertain')
        self.assertEqual(self.service.dispatch(**args, send=lambda event: self.fail('duplicate')), [])

    def test_same_plan_but_risk_changed_is_not_sent(self):
        self.service.evaluate_batch([context()])
        self.service.evaluator = lambda _: {'coverage': [{'action': 'first_buy', 'state': 'blocked'}]}
        result = self.service.dispatch(now=datetime(2026, 9, 23, 2, 0, 1, tzinfo=timezone.utc),
            current_context=lambda event: context(), send=lambda event: self.fail('risk changed'))
        self.assertEqual(result[0]['status'], 'invalidated')

    def test_assembly_snapshot_freshness_later_fills_and_plan_cache(self):
        source = {'account_key': 'a', 'as_of': '2026-09-23T02:00:00+00:00',
                  'snapshot_id': 's', 'decision_id': 'd',
                  'snapshot': {'observed_at': '2026-09-23T01:59:00+00:00', 'verification': 'verified_exact',
                               'total_asset': 10000, 'total_market_value': 1000},
                  'items': [{'symbol': 'x', 'role': 'holding', 'name': 'test',
                             'facts': {'quantity': 100, 'sellable_quantity': 100, 'average_cost': 10}}],
                  'discipline_plans': [{'symbol': 'x', 'plan_kind': 'holding'}]}
        params = dict(quotes={'x': {'price': 10, 'as_of': source['as_of']}}, minutes={}, markets={}, costs={}, policy={'max_quote_age_seconds': 20, 'max_account_age_seconds': 600, 'lot_size_by_symbol': {'x': 100}},
                      compiler_policy={}, repository=self.repo)
        with patch('app.intraday_actions.compiler.compile_action_plan', return_value={'plan': {'hard_stop': 9, 'rules': {'exit': {}}}, 'blockers': []}) as compile_mock:
            first = assemble_contexts(source, **params)[0]
            self.assertTrue(first['account']['quantity_verified'])
            self.assertEqual(first['account']['position_risk'], 100)
            source['items'][0]['facts']['average_cost'] = 7
            self.assertEqual(assemble_contexts(source, **params)[0]['account']['position_risk'], 100)
            source['as_of'] = '2026-09-23T02:01:00+00:00'
            assemble_contexts(source, **params)
            self.assertEqual(compile_mock.call_count, 1)
            source['known_post_snapshot_fills'] = [{'symbol': 'another-stock'}]
            self.assertFalse(assemble_contexts(source, **params)[0]['account']['quantity_verified'])

    def test_real_engine_transition_persists_then_revalidates_without_duplicate(self):
        from app.intraday_actions.engine import evaluate
        c = {'now': '2026-09-23T10:04:05+08:00', 'account_key': 'citics-primary', 'symbol': '600000.SH',
             'name': '合成测试', 'role': 'holding',
             'quote': {'price': 10, 'as_of': '2026-09-23T10:04:04+08:00', 'source': 'fixture', 'bid_available': True},
             'minutes': [{'end_at': f'2026-09-23T10:0{i}:00+08:00', 'close': 10, 'high': 10.1, 'low': 9.9,
                          'vwap': 10, 'volume': 100, 'completed': True, 'source': 'fixture'} for i in (1, 2, 3, 4)],
             'market': {'session_open': True},
             'policy': {'max_quote_age_seconds': 20, 'max_minute_age_seconds': 150, 'max_account_age_seconds': 600,
                        'max_market_age_seconds': 60, 'max_price_deviation_pct': 1},
             'plan': {'id': 'p', 'version': 'v', 'created_at': '2026-09-23T10:00:00+08:00', 'expires_at': '2026-09-23T15:00:00+08:00',
                      'quality_status': 'accepted', 'source_rank': 1, 'hard_stop': 10, 'snapshot_id': 's',
                      'rules': {'exit': {'basis': 'completed_1m', 'kind': 'hard_stop', 'version': '1', 'reference': 10,
                                         'confirmation_minutes': 3, 'quantity': 100}}},
             'account': {'snapshot_id': 's', 'as_of': '2026-09-23T10:00:00+08:00', 'quantity_verified': True,
                         'total_assets': 10000, 'stock_market_value': 1000, 'held_quantity': 100, 'sellable_quantity': 100,
                         'position_risk': 0}}
        service = ActionShadowService(self.repo, evaluator=evaluate)
        first = service.evaluate_batch([c])[0]
        self.assertEqual(first['events'], [])
        for row in c['minutes']:
            row.update(close=9.95, low=9.9)
        c['quote']['price'] = 9.95
        second = service.evaluate_batch([c])[0]
        self.assertEqual(len(second['events']), 1, second['blockers'])
        self.assertEqual(service.evaluate_batch([c])[0]['events'], [])
        sent = []
        result = service.dispatch(now=datetime.fromisoformat(c['now']), current_context=lambda _: deepcopy(c),
                                  send=lambda event: sent.append(event) or True)
        self.assertEqual(result[0]['status'], 'delivered')
        self.assertEqual(len(sent), 1)

    def test_quantity_changed_before_delivery_invalidates(self):
        self.service.evaluate_batch([context()])
        original = self.service.evaluator
        def lower_quantity(c):
            result = original(c)
            result['confirmed_candidates'][0]['quantity'] = 50
            return result
        self.service.evaluator = lower_quantity
        result = self.service.dispatch(now=datetime(2026, 9, 23, 2, 0, 1, tzinfo=timezone.utc),
            current_context=lambda event: context(), send=lambda event: self.fail('stale quantity'))
        self.assertEqual(result[0]['status'], 'invalidated')

    def test_same_plan_changed_price_boundaries_invalidate_queued_card(self):
        for field in ('reference', 'hard_stop', 'max_buy_price', 'min_sell_price'):
            with self.subTest(field=field), closing(ShadowRepository(':memory:')) as repo:
                def bounded(c):
                    result = evaluator(c)
                    result['events'][0].update(reference=10, hard_stop=9, max_buy_price=11, min_sell_price=10.8)
                    return result
                service = ActionShadowService(repo, evaluator=bounded)
                service.evaluate_batch([context()])
                def revised(c):
                    result = bounded(c)
                    result['confirmed_candidates'][0][field] += .01
                    return result
                service.evaluator = revised
                result = service.dispatch(now=datetime(2026, 9, 23, 2, 0, 1, tzinfo=timezone.utc),
                    current_context=lambda event: context(), send=lambda event: self.fail('stale price boundary'))
                self.assertEqual(result[0]['status'], 'invalidated')

    def test_t_actual_import_partial_restart_second_leg_and_duplicate_binding(self):
        for action in ('t_buy_first', 't_sell_first'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'episodes.sqlite3'
                repo = ShadowRepository(path)
                service = ActionShadowService(repo)
                now = datetime.fromisoformat('2026-09-23T10:05:00+08:00')
                episode = {'id': 't1', 'action': action, 'account_key': 'a', 'symbol': '600000.SH',
                           'session_date': '2026-09-23', 'target_quantity': 200, 'old_sellable_quantity': 300,
                           'created_at': '2026-09-23T10:00:00+08:00'}
                service.begin_episode(episode, now=now)
                side = 'buy' if action == 't_buy_first' else 'sell'
                record = {'trade_key': 'fill1', 'account_key': 'a', 'symbol': '600000.SH', 'trade_date': '2026-09-23',
                          'trade_time': '10:01:00', 'quantity': 100, 'price': 10, 'observed_at': '2026-09-23T10:02:00+08:00',
                          'side': side, 'source_sha256': 'sha'}
                result = service.apply_imported_fill(account_key='a', episode_id='t1', leg='first', record=record, now=now)
                self.assertEqual(result['state'], 'first_partial')
                repo.close()
                repo = ShadowRepository(path)
                service = ActionShadowService(repo)
                again = service.apply_imported_fill(account_key='a', episode_id='t1', leg='first', record=record, now=now)
                self.assertEqual(again['first_quantity'], 100)
                service.begin_episode({**episode, 'id': 't2'}, now=now)
                with self.assertRaises(ValueError):
                    service.apply_imported_fill(account_key='a', episode_id='t2', leg='first', record=record, now=now)
                second = {**record, 'trade_key': 'fill2', 'trade_time': '10:03:00',
                          'observed_at': '2026-09-23T10:04:00+08:00', 'side': 'sell' if side == 'buy' else 'buy'}
                result = service.apply_imported_fill(account_key='a', episode_id='t1', leg='second', record=second, now=now)
                self.assertEqual(result['state'], 'partially_completed')
                self.assertEqual(result['open_quantity'], 0)
                self.assertEqual(len(repo.episode('a', 't1')['fills']), 2)
                repo.close()

    def test_partial_plan_enrichment_preserves_existing_boundary(self):
        source = {'account_key': 'a', 'as_of': '2026-09-23T02:00:00+00:00',
                  'snapshot_id': 's', 'decision_id': 'd', 'snapshot': {},
                  'items': [{'symbol': 'x', 'role': 'holding', 'name': 'test', 'facts': {}}],
                  'discipline_plans': [{'symbol': 'x', 'plan_kind': 'holding'}]}
        params = dict(quotes={}, minutes={}, markets={}, costs={}, policy={}, compiler_policy={}, repository=self.repo)
        first_plan = {'id': 'old', 'created_at': source['as_of'], 'rules': {'exit': {'reference': 9}}}
        later_plan = {'id': 'new-must-not-replace-old', 'created_at': '2026-09-23T02:05:00+00:00',
                      'allow_buy': True, 'allow_add': True, 'thesis_valid': True,
                      'rules': {'exit': {'reference': 9}, 'add': {'reference': 10}}}
        with patch('app.intraday_actions.compiler.compile_action_plan', side_effect=[
                {'plan': first_plan, 'blockers': [{'action': 'add', 'code': 'completed_minute_volume_baseline_unavailable'}]},
                {'plan': later_plan, 'blockers': []}]):
            assemble_contexts(source, **params)
            source['as_of'] = later_plan['created_at']
            updated = assemble_contexts(source, **params)[0]['plan']
        self.assertEqual(updated['id'], 'old')
        self.assertEqual(updated['rules']['exit']['created_at'], '2026-09-23T02:00:00+00:00')
        self.assertEqual(updated['rules']['add']['created_at'], '2026-09-23T02:05:00+00:00')
        self.assertTrue(updated['allow_buy'])
        self.assertTrue(updated['allow_add'])
        self.assertTrue(updated['thesis_valid'])

    def test_dispatch_loads_explicit_persisted_episode(self):
        c = context()
        c['t_episode_id'] = 'episode'
        self.repo.save_episode({'id': 'episode', 'account_key': 'a', 'state': 'first_partial', 'fills': [{'fill_id': 'actual'}]})
        observed = []
        def inspect(ctx):
            observed.append(ctx['t_episode']['state'])
            return evaluator(ctx)
        self.service.evaluator = inspect
        self.service.evaluate_batch([c])
        result = self.service.dispatch(now=datetime(2026, 9, 23, 2, 0, 1, tzinfo=timezone.utc),
            current_context=lambda event: c, send=lambda _: True)
        self.assertEqual(result[0]['status'], 'delivered')
        self.assertEqual(observed, ['first_partial', 'first_partial'])

    def test_real_t_partial_fill_restart_second_leg_outbox_and_dispatch(self):
        from test_intraday_action_engine import fixture
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / 't-chain.sqlite3'
            repo = stack.enter_context(closing(ShadowRepository(path)))
            service = ActionShadowService(repo)
            c = fixture('t_buy_first')
            waiting = deepcopy(c)
            for minute in waiting['minutes']:
                minute.update(close=9.95, low=9.9)
            waiting['quote']['price'] = 9.95
            self.assertFalse(service.evaluate_batch([waiting])[0]['events'])
            first = service.evaluate_batch([c])[0]
            self.assertEqual(first['events'][0]['leg'], 'first', first['blockers'])
            c['t_episode_id'] = 'real-chain'
            service.begin_episode({'id': 'real-chain', 'account_key': c['account_key'], 'symbol': c['symbol'],
                'action': 't_buy_first', 'session_date': '2026-09-23', 'target_quantity': 300,
                'old_sellable_quantity': 500, 'created_at': '2026-09-23T10:04:05+08:00'}, now=datetime.fromisoformat(c['now']))
            # No actual fill means repeated ticks cannot manufacture a second leg.
            self.assertFalse(service.evaluate_batch([c])[0]['events'])
            service.apply_imported_fill(account_key=c['account_key'], episode_id='real-chain', leg='first', now=datetime.fromisoformat('2026-09-23T10:04:08+08:00'),
                record={'trade_key': 'immutable-fill', 'account_key': c['account_key'], 'symbol': c['symbol'],
                        'trade_date': '2026-09-23', 'trade_time': '10:04:06', 'quantity': 100, 'price': 10.2,
                        'side': 'buy', 'source_sha256': 'export-hash', 'observed_at': '2026-09-23T10:04:07+08:00'})
            repo.close()
            repo = stack.enter_context(closing(ShadowRepository(path)))
            service = ActionShadowService(repo)
            c['now'] = '2026-09-23T10:07:05+08:00'
            c['quote'].update(price=10.9, as_of='2026-09-23T10:07:04+08:00')
            c['market']['as_of'] = '2026-09-23T10:07:00+08:00'
            c['minutes'].extend(dict(end_at=f'2026-09-23T10:0{i}:00+08:00', close=10.9, high=11,
                low=10.8, vwap=10.8, volume=150, completed=True, source='licensed') for i in (5, 6, 7))
            # Explicit synthetic reconciled account context, not a claimed live snapshot.
            c['account'].update(held_quantity=1100, sellable_quantity=500, position_risk=2090, as_of='2026-09-23T10:04:08+08:00')
            second = service.evaluate_batch([c])[0]
            self.assertEqual(len(second['events']), 1, second['blockers'])
            self.assertEqual(second['events'][0]['leg'], 'second')
            self.assertEqual(second['events'][0]['quantity'], 100)
            sent = []
            delivery = service.dispatch(now=datetime.fromisoformat(c['now']), current_context=lambda _: deepcopy(c), send=lambda event: sent.append(event) or True)
            self.assertEqual([item['status'] for item in delivery], ['expired', 'delivered'])
            self.assertEqual(sent[0]['leg'], 'second')
            self.assertEqual(sent[0]['quantity'], 100)
            self.assertFalse(service.evaluate_batch([c])[0]['events'])
            repo.close()

    def test_real_sell_first_partial_restart_repurchase_outbox_and_dispatch(self):
        from test_intraday_action_engine import fixture
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / 'sell-first-chain.sqlite3'
            repo = stack.enter_context(closing(ShadowRepository(path)))
            service = ActionShadowService(repo)
            c = fixture('t_sell_first')
            waiting = deepcopy(c)
            for minute in waiting['minutes']:
                minute.update(close=10.3, low=10.2)
            waiting['quote']['price'] = 10.3
            self.assertFalse(service.evaluate_batch([waiting])[0]['events'])
            first = service.evaluate_batch([c])[0]
            self.assertEqual(len(first['events']), 1, first['blockers'])
            self.assertEqual(first['events'][0]['leg'], 'first')
            c['t_episode_id'] = 'sell-chain'
            service.begin_episode({'id': 'sell-chain', 'account_key': c['account_key'], 'symbol': c['symbol'],
                'action': 't_sell_first', 'session_date': '2026-09-23', 'target_quantity': 300,
                'old_sellable_quantity': 500, 'created_at': '2026-09-23T10:04:05+08:00'}, now=datetime.fromisoformat(c['now']))
            self.assertFalse(service.evaluate_batch([c])[0]['events'])
            service.apply_imported_fill(account_key=c['account_key'], episode_id='sell-chain', leg='first', now=datetime.fromisoformat('2026-09-23T10:04:08+08:00'),
                record={'trade_key': 'immutable-partial-sell', 'account_key': c['account_key'], 'symbol': c['symbol'],
                        'trade_date': '2026-09-23', 'trade_time': '10:04:06', 'quantity': 100, 'price': 10.3,
                        'side': 'sell', 'source_sha256': 'export-hash', 'observed_at': '2026-09-23T10:04:07+08:00'})
            repo.close()
            repo = stack.enter_context(closing(ShadowRepository(path)))
            service = ActionShadowService(repo)
            c['now'] = '2026-09-23T10:07:05+08:00'
            c['quote'].update(price=9.8, as_of='2026-09-23T10:07:04+08:00')
            c['market']['as_of'] = '2026-09-23T10:07:00+08:00'
            c['minutes'].extend(dict(end_at=f'2026-09-23T10:0{i}:00+08:00', close=9.8, high=9.9,
                low=9.7, vwap=9.7, volume=150, completed=True, source='licensed') for i in (5, 6, 7))
            c['account'].update(held_quantity=900, sellable_quantity=400, stock_market_value=8820,
                                position_risk=720, as_of='2026-09-23T10:04:08+08:00')
            second = service.evaluate_batch([c])[0]
            self.assertEqual(len(second['events']), 1, second['blockers'])
            self.assertEqual(second['events'][0]['leg'], 'second')
            self.assertEqual(second['events'][0]['quantity'], 100)
            self.assertGreater(repo.reserved_cash(c['account_key']), 990)
            sent = []
            delivery = service.dispatch(now=datetime.fromisoformat(c['now']), current_context=lambda _: deepcopy(c), send=lambda event: sent.append(event) or True)
            self.assertEqual([item['status'] for item in delivery], ['expired', 'delivered'])
            self.assertEqual(sent[0]['action'], 't_sell_first')
            self.assertEqual(sent[0]['leg'], 'second')
            self.assertEqual(sent[0]['quantity'], 100)
            self.assertFalse(service.evaluate_batch([c])[0]['events'])
            # Delivery isn't a buy fill; only this separate immutable record closes the open leg.
            self.assertEqual(repo.episode(c['account_key'], 'sell-chain')['open_quantity'], 100)
            bought = service.apply_imported_fill(account_key=c['account_key'], episode_id='sell-chain', leg='second', now=datetime.fromisoformat('2026-09-23T10:07:09+08:00'),
                record={'trade_key': 'immutable-repurchase', 'account_key': c['account_key'], 'symbol': c['symbol'],
                        'trade_date': '2026-09-23', 'trade_time': '10:07:07', 'quantity': 100, 'price': 9.8,
                        'side': 'buy', 'source_sha256': 'export-hash', 'observed_at': '2026-09-23T10:07:08+08:00'})
            self.assertEqual(bought['open_quantity'], 0)
            repo.close()


if __name__ == '__main__':
    unittest.main()

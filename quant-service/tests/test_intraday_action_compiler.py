"""Independent minute-plan compilation must preserve the source contract."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from app.intraday_actions.compiler import compile_action_plan


NOW = datetime.fromisoformat('2026-09-23T10:00:00+08:00')


def fixture(kind='new_buy'):
    return {
        'plan_id': 'p1', 'account_key': 'a', 'symbol': '600000.SH',
        'source_rank': 2, 'source_rank_source': 'fixture-formal-priority',
        'plan_kind': kind, 'status': 'active', 'quality': [{'passed': True}],
        'created_at': '2026-09-22T18:01:00+08:00',
        'as_of_at': '2026-09-22T18:00:00+08:00',
        'valid_until': '2026-09-24T15:00:00+08:00',
        'inputs_hash': 'source-evidence-hash', 'stage': 'trend_hold',
        'metrics': {'trading_date': '2026-09-22', 'ma5': 10.0, 'prior_high': 10.0,
                    'entry': {'lane_reference': 10.0}},
        'sizing': {'hard_stop': 9.0, 'sizing_price': 10.5, 'recommended_shares': 500,
                   'risk_per_trade_pct': 5,
                   'current_shares': 200 if kind == 'holding' else 0},
        'position': {'snapshot_id': 's1', 'quantity': 200, 'sellable_quantity': 100}
                    if kind == 'holding' else None,
        'evidence_refs': ['recommendation_decision:d1'],
        'lines': [{'kind': 'hard_stop', 'metric': 'minute_close', 'op': '<', 'price': 9.0,
                   'confirm': {'basis': 'minute', 'bars': 3}, 'extra': [],
                   'action': {'type': 'exit_all'}}],
    }


POLICY = {'version': 'test-policy-v1', 'source': 'explicit-test-policy',
          'confirmation_minutes': 3, 'volume_baseline_minutes': 3,
          'min_volume_ratio': 1.2, 'sector_min_change': 0,
          'risk_budget_pct': 0.05, 'thesis_valid': True,
          'thesis_source': 'explicit-test-thesis'}


def minutes():
    return [{'end_at': (NOW - timedelta(minutes=i)).isoformat(), 'volume': 100 * (4-i),
             'completed': True, 'source': 'licensed-minute'} for i in (3, 2, 1)]


def compile_plan(raw=None, **changes):
    raw = raw or fixture()
    args = dict(account_key='a', symbol='600000.SH', role='holding' if raw['plan_kind'] == 'holding'
                else 'recommendation', snapshot_id='s1', decision_id='d1', compiled_at=NOW,
                minutes=minutes(), policy=POLICY)
    args.update(changes)
    return compile_action_plan(raw, **args)


class CompilerTests(unittest.TestCase):
    def test_buy_is_independent_and_source_unmodified(self):
        raw = fixture(); before = deepcopy(raw)
        result = compile_plan(raw)
        self.assertEqual(raw, before)
        plan = result['plan']
        self.assertTrue(plan['research_only'])
        self.assertEqual(plan['live_effect'], 'none')
        rule = plan['rules']['first_buy']
        self.assertEqual(rule['basis'], 'completed_1m')
        self.assertEqual(rule['reference'], 10)
        self.assertEqual(rule['volume_baseline'], 200)
        self.assertEqual(rule['quantity'], 500)
        self.assertEqual(plan['expires_at'], '2026-09-23T15:00:00+08:00')
        self.assertEqual(plan['provenance']['source_plan_id'], 'p1')

    def test_holding_compiles_add_and_original_minute_exit(self):
        rules = compile_plan(fixture('holding'))['plan']['rules']
        self.assertEqual(rules['add']['quantity'], 300)
        self.assertEqual(rules['exit']['reference'], 9)
        self.assertEqual(rules['exit']['quantity'], 200)

    def test_daily_exit_not_promoted(self):
        raw = fixture('holding'); raw['lines'][0]['confirm']['basis'] = 'daily'
        raw['lines'][0]['metric'] = 'daily_close'
        self.assertNotIn('exit', compile_plan(raw)['plan']['rules'])

    def test_no_add_daily_is_not_released_by_current_price(self):
        raw = fixture('holding')
        raw['lines'].append({'kind': 'no_add', 'confirm': {'basis': 'daily'}, 'price': 8,
                             'action': {'type': 'block_add'}})
        result = compile_plan(raw)
        self.assertNotIn('add', result['plan']['rules'])
        self.assertIn('source_no_add_not_released', [b['code'] for b in result['blockers']])

    def test_identity_and_quality_fail_closed(self):
        for field, value in [('status', 'rejected_by_quality'), ('quality', []),
                             ('quality', [{'passed': False}]), ('account_key', 'other'),
                             ('symbol', '600001.SH')]:
            with self.subTest(field=field, value=value):
                raw = fixture(); raw[field] = value
                self.assertIsNone(compile_plan(raw)['plan'])

    def test_snapshot_and_decision_bindings(self):
        self.assertIsNone(compile_plan(fixture('holding'), snapshot_id='different')['plan'])
        self.assertIsNone(compile_plan(decision_id='different')['plan'])

    def test_source_cannot_come_from_future(self):
        for field in ('created_at', 'as_of_at'):
            raw = fixture(); raw[field] = (NOW + timedelta(seconds=1)).isoformat()
            self.assertIsNone(compile_plan(raw)['plan'])

    def test_forming_or_today_structure_cannot_drive_buy(self):
        raw = fixture(); raw['metrics']['trading_date'] = NOW.date().isoformat()
        result = compile_plan(raw)
        self.assertNotIn('first_buy', result['plan']['rules'])
        self.assertIn('settled_structure_required', [b['code'] for b in result['blockers']])

    def test_missing_volume_or_policy_does_not_borrow_daily_volume(self):
        for override in ({'minutes': []}, {'policy': None}):
            result = compile_plan(**override)
            self.assertNotIn('first_buy', result['plan']['rules'])

    def test_volume_rejects_duplicates_gaps_incomplete_and_future(self):
        for case in ('duplicate', 'gap', 'incomplete', 'future'):
            rows = minutes()
            if case == 'duplicate': rows[1]['end_at'] = rows[0]['end_at']
            if case == 'gap': rows[1]['end_at'] = (NOW-timedelta(minutes=4)).isoformat()
            if case == 'incomplete': rows[-1]['completed'] = False
            if case == 'future': rows[-1]['end_at'] = (NOW+timedelta(minutes=1)).isoformat()
            with self.subTest(case=case):
                self.assertNotIn('first_buy', compile_plan(minutes=rows)['plan']['rules'])

    def test_no_t_prices_or_fee_assumptions_are_invented(self):
        result = compile_plan(fixture('holding'))
        self.assertNotIn('t_buy_first', result['plan']['rules'])
        self.assertNotIn('t_sell_first', result['plan']['rules'])
        self.assertEqual(len([b for b in result['blockers'] if b['code'] == 'explicit_t_plan_required']), 2)

    def test_idempotent_frozen_inputs(self):
        self.assertEqual(compile_plan()['plan'], compile_plan()['plan'])

    def test_add_needs_thesis_confirmation(self):
        policy = {k: v for k, v in POLICY.items() if not k.startswith('thesis')}
        self.assertNotIn('add', compile_plan(fixture('holding'), policy=policy)['plan']['rules'])

    def test_risk_policy_is_authoritative_and_old_sizing_not_a_new_order_cap(self):
        raw = fixture(); raw['sizing']['risk_per_trade_pct'] = 1
        result = compile_plan(raw)
        self.assertEqual(result['plan']['risk_budget_pct'], 0.05)
        self.assertNotIn('first_buy', result['plan']['rules'])
        self.assertIn('source_sizing_risk_policy_not_current', [b['code'] for b in result['blockers']])

    def test_holding_does_not_need_fake_rank(self):
        raw = fixture('holding'); raw.pop('source_rank'); raw.pop('source_rank_source')
        self.assertIn('exit', compile_plan(raw)['plan']['rules'])
        self.assertIn('add', compile_plan(raw)['plan']['rules'])

    def test_explicit_structural_t_and_reduction_can_compile(self):
        raw = fixture('holding'); raw['metrics'].update(recent_low=9.5, prior_high=11, prev_close=10.5)
        policy = {**POLICY, 't': {'source': 'explicit-research-T-v1', 'range_low_field': 'recent_low',
                                 'range_high_field': 'prior_high', 'max_buy_price_field': 'ma5', 'quantity': 100,
                                 'min_sell_price_field': 'prev_close',
                                 'min_net_profit': 10, 'min_pullback_pct': 0.5},
                  'reduction': {'source': 'explicit-research-reduce-v1', 'reference_field': 'ma5', 'quantity': 100},
                  'costs': {'source': 'verified-account-costs-fixture', 'as_of': NOW.isoformat(),
                            'commission_rate': 0.0003, 'min_commission': 5, 'stamp_tax_sell_rate': 0.0005,
                            'transfer_rate': 0.00001, 'slippage_bps': 5}}
        rules = compile_plan(raw, policy=policy)['plan']['rules']
        self.assertEqual(set(rules), {'exit', 'reduce', 'add', 't_buy_first', 't_sell_first'})
        self.assertEqual(rules['t_buy_first']['reference'], 9.5)
        self.assertEqual(rules['t_buy_first']['target_price'], 11)
        self.assertEqual(rules['t_buy_first']['max_buy_price'], 10)
        self.assertEqual(rules['t_sell_first']['second_leg']['reference'], 9.5)
        self.assertEqual(rules['t_sell_first']['min_sell_price'], 10.5)
        self.assertEqual(rules['t_sell_first']['min_sell_price_source'], 'metrics.prev_close')
        policy['costs'] = {}
        self.assertNotIn('t_sell_first', compile_plan(raw, policy=policy)['plan']['rules'])
        policy['t']['range_high_field'] = 'low10_close'
        self.assertNotIn('t_buy_first', compile_plan(raw, policy=policy)['plan']['rules'])

    def test_compiled_t_rules_produce_actual_engine_events_without_regular_add(self):
        from test_intraday_action_engine import fixture as engine_fixture, arm
        from app.intraday_actions.engine import evaluate
        for action in ('t_buy_first', 't_sell_first'):
            with self.subTest(action=action):
                ctx = engine_fixture(action)
                raw = fixture('holding'); raw['account_key'] = 'citics-primary'
                raw['position']['snapshot_id'] = 'snapshot'
                raw['sizing']['recommended_shares'] = raw['sizing']['current_shares']
                raw['metrics'].update(ma5=10.4 if action == 't_buy_first' else 9.9,
                                      recent_low=10 if action == 't_buy_first' else 9.8,
                                      prior_high=10.8 if action == 't_buy_first' else 10.5,
                                      prev_close=10.5 if action == 't_buy_first' else 10.2)
                policy = {**POLICY, 't': {'source': 'explicit-test-T', 'range_low_field': 'recent_low',
                          'range_high_field': 'prior_high', 'max_buy_price_field': 'ma5', 'quantity': 100,
                          'min_sell_price_field': 'prev_close',
                          'min_net_profit': 10, 'min_pullback_pct': 0.5}, 'costs': ctx['account']['costs']}
                compiled = compile_plan(raw, account_key='citics-primary', snapshot_id='snapshot', policy=policy)
                self.assertNotIn('add', compiled['plan']['rules'])
                ctx['plan'] = compiled['plan']
                for row in ctx['minutes']:
                    row['volume'] = 400
                result = evaluate(arm(ctx))
                self.assertEqual([e['action'] for e in result['events']], [action], result['blockers'])
                if action == 't_sell_first':
                    self.assertEqual(result['events'][0]['min_sell_price'], 10.2)
                    for field in (None, 'recent_low', 'unknown'):
                        invalid = deepcopy(policy)
                        invalid['t']['min_sell_price_field'] = field
                        result = compile_plan(raw, account_key='citics-primary', snapshot_id='snapshot', policy=invalid)
                        self.assertNotIn(action, result['plan']['rules'])
                        self.assertIn('frozen_t_sell_floor_missing_or_invalid', [b['code'] for b in result['blockers']])
                policy.pop('thesis_valid')
                if action == 't_buy_first':
                    self.assertNotIn(action, compile_plan(raw, account_key='citics-primary',
                                     snapshot_id='snapshot', policy=policy)['plan']['rules'])
                policy['t'].pop('max_buy_price_field')
                self.assertNotIn(action, compile_plan(raw, account_key='citics-primary',
                                 snapshot_id='snapshot', policy=policy)['plan']['rules'])

    def test_preview_identity_is_never_a_production_record(self):
        raw = fixture('holding'); raw['plan_key'] = 'local-key'
        raw.pop('plan_id'); raw.pop('created_at')
        self.assertIsNone(compile_plan(raw)['plan'])
        result = compile_plan(raw, local_preview=True)
        self.assertEqual(result['plan']['mode'], 'local_preview')
        self.assertFalse(result['plan']['production_eligible'])
        self.assertEqual(result['provenance']['source_plan_id'], 'local-preview:local-key')
        raw['status'] = 'rejected_by_quality'
        self.assertIsNone(compile_plan(raw, local_preview=True)['plan'])

    def test_available_real_dryrun_shape_preserves_quality_and_identity(self):
        import json
        from pathlib import Path
        folder = Path('G:/StockPlatform/reports/intraday-actions/2026-09-23/discipline-dry-run/2026-09-22')
        files = list(folder.glob('*.json'))
        if not files:
            self.skipTest('local read-only real-shape evidence is not installed')
        accepted = rejected = 0
        for path in files:
            report = json.loads(path.read_text(encoding='utf-8-sig'))
            raw = report['plan']
            args = dict(account_key=raw['account_key'], symbol=raw['symbol'],
                        role='holding' if raw['plan_kind'] == 'holding' else 'recommendation',
                        snapshot_id=(raw.get('position') or {}).get('snapshot_id'),
                        decision_id=next((r.split(':', 1)[1] for r in raw['evidence_refs']
                                          if r.startswith('recommendation_decision:')), None),
                        compiled_at=NOW, minutes=minutes(), policy=POLICY)
            self.assertIsNone(compile_action_plan(raw, **args)['plan'])
            result = compile_action_plan(raw, **args, local_preview=True)
            if raw['status'] == 'active':
                accepted += 1
                self.assertIn('exit', result['plan']['rules'])
                self.assertEqual(result['plan']['mode'], 'local_preview')
            else:
                rejected += 1
                self.assertIsNone(result['plan'])
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 0)

    def test_compiled_plan_really_drives_engine_buy_and_add(self):
        from app.intraday_actions.engine import evaluate
        for kind, action in [('new_buy', 'first_buy'), ('holding', 'add')]:
            with self.subTest(action=action):
                raw = fixture(kind); raw['account_key'] = 'citics-primary'
                plan = compile_plan(raw, account_key='citics-primary')['plan']
                after = [{**r, 'close': 10.2, 'low': 10.1, 'high': 10.3, 'vwap': 10.1}
                         for r in minutes()]
                after += [{'end_at': (NOW + timedelta(minutes=i)).isoformat(), 'completed': True,
                           'source': 'licensed-minute', 'volume': 400, 'close': 10.2,
                           'low': 10.1, 'high': 10.3, 'vwap': 10.1} for i in (0, 1, 2, 3)]
                now = NOW + timedelta(minutes=3)
                context = {'now': now.isoformat(), 'account_key': 'citics-primary', 'symbol': raw['symbol'],
                           'name': 'fixture', 'role': 'holding' if kind == 'holding' else 'recommendation',
                           'formal_recommendation': True, 'plan': plan, 'minutes': after,
                           'quote': {'price': 10.2, 'as_of': now.isoformat(), 'source': 'licensed-minute',
                                     'bid_available': True, 'ask_available': True},
                           'market': {'session_open': True, 'sector_confirmed': True, 'sector_key': 'bank',
                                      'sector_change_pct': 1, 'as_of': now.isoformat(), 'source': 'licensed-minute'},
                           'policy': {'max_quote_age_seconds': 20, 'max_minute_age_seconds': 150,
                                      'max_account_age_seconds': 600, 'max_market_age_seconds': 90,
                                      'max_price_deviation_pct': 0.5},
                           'account': {'snapshot_id': 's1', 'as_of': now.isoformat(), 'quantity_verified': True,
                                       'held_quantity': 200 if kind == 'holding' else 0,
                                       'sellable_quantity': 100 if kind == 'holding' else 0,
                                       'position_cost': 10, 'position_risk': 240 if kind == 'holding' else 0,
                                       'total_assets': 100000, 'stock_market_value': 10000,
                                       'allocated_buy_cash': 0, 'lot_size': 100,
                                       'costs': {'source': 'fixture', 'as_of': now.isoformat(),
                                                 'commission_rate': 0.0003, 'min_commission': 5,
                                                 'stamp_tax_sell_rate': 0.0005, 'transfer_rate': 0.00001,
                                                 'slippage_bps': 5}}}
                prior_context = deepcopy(context)
                prior_context['now'] = (NOW+timedelta(minutes=2)).isoformat()
                prior_context['quote']['as_of'] = prior_context['now']
                prior_context['market']['as_of'] = prior_context['now']
                prior_context['account']['as_of'] = prior_context['now']
                prior_context['account']['costs']['as_of'] = prior_context['now']
                context['previous_states'] = evaluate(prior_context)['states']
                result = evaluate(context)
                self.assertEqual([e['action'] for e in result['events']], [action], result['blockers'])


if __name__ == '__main__':
    unittest.main()

import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch
from types import SimpleNamespace

from app.trade_discipline.alerts_eligibility import AlertScope


class CoverageTests(unittest.TestCase):
    def scope(self, plans=(), **kwargs):
        return AlertScope(account_key='test', plans=plans,
                          expected_holdings=('a', 'b', 'c', 'd', 'e'), **kwargs)

    def test_two_of_five_is_not_healthy(self):
        plans = tuple((s, SimpleNamespace(symbol=s, plan_kind='holding')) for s in ['a', 'b'])
        scope = self.scope(plans)
        self.assertTrue(scope.has_coverage_gaps)
        self.assertEqual(scope.coverage['holdings']['covered'], 2)
        self.assertEqual(scope.coverage['holdings']['expected'], 5)
        self.assertEqual(scope.coverage['holdings']['missing_symbols'], ['c', 'd', 'e'])

    def test_snapshot_and_plan_changes_invalidate_fingerprint(self):
        base = self.scope(snapshot_id='one')
        self.assertNotEqual(base.fingerprint, self.scope(snapshot_id='two').fingerprint)
        self.assertNotEqual(base.fingerprint, self.scope(snapshot_id='one', plans=(
            ('new', SimpleNamespace(symbol='a', plan_kind='holding')),)).fingerprint)

    def test_held_recommendation_uses_holding_plan(self):
        plan = ('a-plan', SimpleNamespace(symbol='a', plan_kind='holding'))
        scope = AlertScope(account_key='test', plans=(plan,), expected_holdings=('a',),
                           expected_recommendations=('a',))
        self.assertFalse(scope.has_coverage_gaps)

    def test_daily_cache_checks_scope_instead_of_only_date(self):
        from app.trade_discipline.alerts_runtime import _daily_cycle_completed
        scope = self.scope(snapshot_id='new')
        class DB:
            @contextmanager
            def transaction(self):
                yield self
            def execute(self, *args):
                return self
            def fetchone(self):
                return {'last_session_date': date(2026, 9, 21),
                        'details': {'daily_completed': True, 'scope_fingerprint': 'old'}}
        with patch('app.trade_discipline.alerts_runtime._load_scope', return_value=scope):
            self.assertFalse(_daily_cycle_completed(DB(), 'test', date(2026, 9, 21)))

import copy
import unittest
from datetime import datetime, timezone

from app.broker_fact_sync_rules import validate_exact_totals, validate_quote, window_status
from app.personal_decision_contracts import BrokerPortfolioSnapshotInput


class BrokerFactRulesTests(unittest.TestCase):
    def test_public_contract_cannot_claim_exact_without_totals(self):
        base = dict(account_key='citics-primary', source='citics_mumu', source_snapshot_key='x',
                    observed_at='2026-09-10T15:20:00+08:00', verification='verified_exact', positions=[])
        with self.assertRaises(ValueError): BrokerPortfolioSnapshotInput.model_validate(base)
        with self.assertRaises(ValueError): BrokerPortfolioSnapshotInput.model_validate(base | {'total_asset': 2000, 'cash': 1000, 'total_market_value': 1000})
        BrokerPortfolioSnapshotInput.model_validate(base | {'total_asset': 2000, 'cash': 2000, 'total_market_value': 0})

    def test_complete_and_empty(self):
        validate_exact_totals({'total_assets': 120, 'market_value': 100, 'available_cash': 10},
                              [{'quantity': 10, 'available_quantity': 0, 'price': 10, 'market_value': 100}])
        validate_exact_totals({'total_assets': 120, 'market_value': 0, 'available_cash': 110}, [])

    def test_reject_missing_or_false_empty(self):
        for account, positions in [({}, []), ({'total_assets': 120, 'market_value': 100, 'available_cash': 20}, [])]:
            with self.assertRaises(ValueError):
                validate_exact_totals(account, positions)

    def test_quote_date_and_price(self):
        now = datetime.fromisoformat('2026-09-10T15:20:00+08:00')
        q = {'price': 10, 'trade_time': '20260910150000'}
        validate_quote(q, 10, now, 'close')
        for bad in [{'price': 9, 'trade_time': q['trade_time']}, {'price': 10, 'trade_time': '20260909150000'}]:
            with self.assertRaises(ValueError):
                validate_quote(bad, 10, now, 'close')

    def test_calendar_not_weekday_assumption(self):
        now = datetime.fromisoformat('2026-10-01T12:00:00+08:00')
        self.assertEqual(window_status(now, 'midday', False), 'closed')
        with self.assertRaises(ValueError): window_status(now, 'midday', None)
        self.assertEqual(window_status(now, 'midday', True), 'allowed')
        with self.assertRaises(ValueError): window_status(now, 'close', True)


if __name__ == '__main__': unittest.main()

import tempfile
import unittest
from pathlib import Path

from app.intraday_actions.repository import ShadowRepository


class ShadowRepositoryTests(unittest.TestCase):
    def test_restart_preserves_states_and_idempotent_outbox(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'shadow.sqlite3'
            repo = ShadowRepository(path)
            event = {'event_key': 'e', 'account_key': 'a', 'symbol': 's', 'action': 'first_buy'}
            with repo.transaction():
                repo.save_states('a', 's', 'p', '1', {'first_buy': {'state': 'waiting'}})
                self.assertTrue(repo.enqueue(event, expires_at='2026-09-23T02:00:00+00:00', reserve_cash=100))
                self.assertFalse(repo.enqueue(event, expires_at='2026-09-23T02:00:00+00:00', reserve_cash=100))
            repo.close()
            repo = ShadowRepository(path)
            self.assertEqual(repo.states('a', 's', 'p', '1')['first_buy']['state'], 'waiting')
            self.assertEqual(repo.reserved_cash('a'), 100)
            self.assertEqual(len(repo.pending()), 1)
            repo.close()

    def test_transaction_rolls_back_states_and_events_together(self):
        repo = ShadowRepository(':memory:')
        with self.assertRaises(RuntimeError):
            with repo.transaction():
                repo.save_states('a', 's', 'p', '1', {'exit': {}})
                repo.enqueue({'event_key': 'e', 'account_key': 'a'}, expires_at='x', reserve_cash=0)
                raise RuntimeError('crash')
        self.assertEqual(repo.states('a', 's', 'p', '1'), {})
        self.assertEqual(repo.pending(), [])

    def test_delivery_does_not_claim_fill_or_release_cash(self):
        repo = ShadowRepository(':memory:')
        with repo.transaction():
            repo.enqueue({'event_key': 'e', 'account_key': 'a'}, expires_at='x', reserve_cash=200)
            repo.mark('e', 'delivered')
        self.assertEqual(repo.reserved_cash('a'), 200)
        self.assertEqual(repo.pending(), [])
        with repo.transaction():
            repo.resolve_reservation('e', reason='user_cancelled', evidence_id='user:1')
        self.assertEqual(repo.reserved_cash('a'), 0)

    def test_claim_is_atomic_and_crash_is_quarantined(self):
        repo = ShadowRepository(':memory:')
        repo.enqueue({'event_key': 'e', 'account_key': 'a'}, expires_at='x', reserve_cash=100)
        self.assertTrue(repo.claim('e'))
        self.assertFalse(repo.claim('e'))
        self.assertEqual(repo.quarantine_interrupted_sends(), 1)
        self.assertEqual(repo.pending(), [])
        self.assertEqual(repo.reserved_cash('a'), 100)

    def test_fill_cannot_be_consumed_by_two_episodes(self):
        repo = ShadowRepository(':memory:')
        fill = {'fill_id': 'real:1', 'verified': True, 'quantity': 100}
        with repo.transaction():
            self.assertTrue(repo.consume_fill('a', 't1', fill))
            self.assertFalse(repo.consume_fill('a', 't1', fill))
            with self.assertRaises(ValueError):
                repo.consume_fill('a', 't2', fill)

    def test_compiled_plan_timestamp_is_immutable(self):
        repo = ShadowRepository(':memory:')
        repo.cache_plan('p', {'plan': {'created_at': 'first'}})
        repo.cache_plan('p', {'plan': {'created_at': 'second'}})
        self.assertEqual(repo.cached_plan('p')['plan']['created_at'], 'first')


if __name__ == '__main__':
    unittest.main()

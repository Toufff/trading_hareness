from datetime import date, datetime, timezone
from decimal import Decimal as D
import os
import unittest
from unittest.mock import patch

from app.daily_bar_repository import upsert_daily_bar
from app.daily_bar_batch_repository import upsert_daily_bars
from app.index_amount_repair import repair_index_amounts
from app.request_models import DailyBar


@unittest.skipUnless(os.environ.get('PGDATABASE', '').startswith('stock_audit_test_'), 'requires isolated audit database')
class IndexRepairIntegrationTest(unittest.TestCase):
    def test_evidence_repair_is_idempotent_and_preserves_availability(self):
        from app.database import Database
        db = Database()
        day = date(2098, 9, 21)
        available = datetime(2098, 9, 21, 8, tzinfo=timezone.utc)
        bar = DailyBar(symbol='399001.SZ', trading_date=day, close=D('13730.02'),
                       volume=D('631708071'), amount=D('1084694363.40319'),
                       source='eastmoney_free', available_at=available)
        try:
            with db.transaction() as conn:
                # Reproduce the old actual writer's incorrect isolation.
                with patch('app.daily_bar_repository.daily_amount_unit_mismatch', return_value=True):
                    upsert_daily_bar(conn, bar)
                self.assertEqual(repair_index_amounts(conn, start=day, end=day)['eligible'], 1)
                self.assertIsNone(conn.execute('SELECT amount FROM quant.canonical_bars_daily WHERE symbol=%s', (bar.symbol,)).fetchone()['amount'])
                self.assertEqual(repair_index_amounts(conn, start=day, end=day, apply=True)['eligible'], 1)
                self.assertEqual(repair_index_amounts(conn, start=day, end=day, apply=True)['eligible'], 0)
                actual = conn.execute('SELECT amount,available_at,quality_status FROM quant.canonical_bars_daily WHERE symbol=%s', (bar.symbol,)).fetchone()
                self.assertEqual(actual['amount'], bar.amount)
                self.assertEqual(actual['available_at'], available)
                self.assertEqual(actual['quality_status'], 'fresh')
                # Both current live write paths preserve the amount.
                upsert_daily_bar(conn, bar)
                upsert_daily_bars(conn, [bar])
                self.assertEqual(conn.execute('SELECT amount FROM quant.canonical_bars_daily WHERE symbol=%s', (bar.symbol,)).fetchone()['amount'], bar.amount)
        finally:
            db.close()

from __future__ import annotations

import asyncio
import unittest
from datetime import date

from app.async_event_read_repository import market_events as async_market_events
from app.event_read_model import market_events


class _Result:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class _AsyncResult:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.row


class _Tx:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Database:
    def __init__(self, asynchronous=False):
        self.calls = []
        self.asynchronous = asynchronous

    def transaction(self):
        return _Tx(self)

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT COUNT"):
            return _AsyncResult(row={"total": 1})
        return _AsyncResult(rows=[{"event_id": "e1", "event_type": "limit_up_pool", "body": "{}"}])

    def execute_sync(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT COUNT"):
            return _Result(row={"total": 1})
        return _Result(rows=[{"event_id": "e1", "event_type": "limit_up_pool", "body": "{}"}])


class _SyncDatabase(_Database):
    def execute(self, sql, params=()):
        return self.execute_sync(sql, params)


class MarketEventReadTests(unittest.TestCase):
    def test_sync_projection_preserves_body_and_applies_exchange_date(self):
        database = _SyncDatabase()
        payload = market_events(database, "000001.sz", "limit_up_pool", date(2026, 8, 31), 900, -2)
        self.assertEqual(payload["items"][0]["event_id"], "e1")
        self.assertTrue(payload["research_only"])
        self.assertEqual(payload["limit"], 500)
        self.assertIn("Asia/Shanghai", database.calls[0][0])

    def test_async_projection_uses_native_connection(self):
        database = _Database(asynchronous=True)
        payload = asyncio.run(async_market_events(database, None, None, None, 10, 0))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(database.calls), 2)


if __name__ == "__main__":
    unittest.main()

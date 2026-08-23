from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import unittest

from app.intraday_order_book_runtime import (
    IntradayOrderBookRuntimeDependencies,
    run_intraday_order_book_runtime_loop,
)


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))
        return _Result(self.rows if "intraday_watchlists" in query else [])


class _Database:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    @contextmanager
    def transaction(self):
        yield self.connection


class IntradayOrderBookRuntimeTests(unittest.TestCase):
    def test_runtime_keeps_bounded_read_and_depth_only_retention(self) -> None:
        database = _Database([{"symbol": "000002.SZ"}, {"symbol": "000001.SZ"}])
        calls = []
        observed_at = datetime(2026, 8, 22, 7, tzinfo=timezone.utc)

        async def run_database(operation, *args, **kwargs):
            calls.append(operation.__name__)
            return operation(*args)

        async def realtime_session():
            return True, "continuous_auction"

        async def open_capabilities(*_):
            return set()

        async def storage_allowed():
            return True, {"state": "healthy"}

        async def capture(_):
            return {"status": "completed"}

        async def run_loop(**kwargs):
            self.assertEqual(await kwargs["load_symbols"](), ["000002.SZ", "000001.SZ"])
            await kwargs["prune_before"](observed_at, 7)
            self.assertEqual(kwargs["interval_seconds"](), 3.0)
            self.assertEqual(kwargs["retention_days"](), 7)

        asyncio.run(run_intraday_order_book_runtime_loop(IntradayOrderBookRuntimeDependencies(
            database=database, run_database=run_database, max_symbols=lambda: 40,
            realtime_session=realtime_session, open_capabilities=open_capabilities,
            storage_allowed=storage_allowed, capture=capture, interval_seconds=lambda: 3.0,
            retention_days=lambda: 7, run_loop=run_loop,
        )))
        self.assertEqual(calls, ["load_watches", "prune"])
        select_query, select_params = database.connection.executed[0]
        delete_query, delete_params = database.connection.executed[1]
        self.assertIn("intraday_watchlists", select_query)
        self.assertEqual(select_params, (40,))
        self.assertIn("source_name='tencent_order_book'", delete_query)
        self.assertEqual(delete_params, (datetime(2026, 8, 15, 7, tzinfo=timezone.utc),))


if __name__ == "__main__":
    unittest.main()

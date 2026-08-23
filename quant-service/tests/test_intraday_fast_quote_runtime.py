from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import unittest

from app.intraday_fast_quote_runtime import (
    IntradayFastQuoteRuntimeDependencies,
    run_intraday_fast_quote_runtime_loop,
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


class IntradayFastQuoteRuntimeTests(unittest.TestCase):
    def test_runtime_keeps_bounded_watch_read_and_rt_k_only_retention(self) -> None:
        database = _Database([
            {"symbol": "000002.SZ", "rank": 2},
            {"symbol": "000001.SZ", "rank": 1},
        ])
        calls = []
        cutoff = datetime(2026, 8, 21, 7, tzinfo=timezone.utc)

        async def run_database(operation, *args, **kwargs):
            calls.append(operation.__name__)
            return operation(*args)

        async def run_loop(**kwargs):
            self.assertEqual(await kwargs["load_symbols"](), ["000001.SZ", "000002.SZ"])
            await kwargs["prune_before"](cutoff)
            self.assertEqual(kwargs["interval_seconds"](), 1.0)
            self.assertEqual(kwargs["max_in_flight"](), 20)
            self.assertEqual(kwargs["retention_days"](), 7)

        async def realtime_session():
            return True, "continuous_auction"

        async def storage_allowed():
            return True, {"state": "healthy"}

        async def capture(_):
            return {"status": "completed"}

        asyncio.run(run_intraday_fast_quote_runtime_loop(IntradayFastQuoteRuntimeDependencies(
            database=database, run_database=run_database, max_symbols=lambda: 40,
            watch_priority_key=lambda row: row["rank"], realtime_session=realtime_session,
            high_frequency_window=lambda _: True, storage_allowed=storage_allowed, capture_quote=capture,
            observe_completed=lambda *_: None, interval_seconds=lambda: 1.0,
            max_in_flight=lambda: 20, retention_days=lambda: 7, run_loop=run_loop,
        )))
        self.assertEqual(calls, ["load_watches", "prune"])
        select_query, select_params = database.connection.executed[0]
        delete_query, delete_params = database.connection.executed[1]
        self.assertIn("intraday_watchlists", select_query)
        self.assertEqual(select_params, (40,))
        self.assertIn("source_name='tushare_super_get_rt_k'", delete_query)
        self.assertEqual(delete_params, (cutoff,))


if __name__ == "__main__":
    unittest.main()

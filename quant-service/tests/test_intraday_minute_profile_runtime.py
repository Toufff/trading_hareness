from __future__ import annotations

import asyncio
from contextlib import contextmanager
import unittest

from app.intraday_minute_profile_runtime import (
    IntradayMinuteProfileRuntimeDependencies,
    run_intraday_minute_profile_runtime_loop,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))
        return _Result(self.rows)


class _Database:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    @contextmanager
    def transaction(self):
        yield self.connection


class IntradayMinuteProfileRuntimeTests(unittest.TestCase):
    def test_runtime_keeps_close_scheduler_and_bounded_priority_watch_read(self) -> None:
        database = _Database([
            {"symbol": "000002.SZ", "rank": 2},
            {"symbol": "000001.SZ", "rank": 1},
        ])
        calls = []

        async def run_database(operation, *args, **kwargs):
            calls.append(operation.__name__)
            return operation(*args)

        async def calendar_open(_):
            return True

        async def storage_allowed():
            return True, {"state": "healthy"}

        async def capture(_):
            return {"status": "completed"}

        async def run_loop(**kwargs):
            self.assertEqual(kwargs["sleep_seconds"], 30)
            self.assertIs(kwargs["calendar_open"], calendar_open)
            self.assertEqual(await kwargs["load_symbols"](), ["000001.SZ", "000002.SZ"])

        asyncio.run(run_intraday_minute_profile_runtime_loop(IntradayMinuteProfileRuntimeDependencies(
            database=database, run_database=run_database, max_symbols=lambda: 40,
            watch_priority_key=lambda row: row["rank"], calendar_open=calendar_open,
            storage_allowed=storage_allowed, capture=capture, run_loop=run_loop,
        )))
        self.assertEqual(calls, ["load_watches"])
        query, params = database.connection.executed[0]
        self.assertIn("intraday_watchlists", query)
        self.assertEqual(params, (40,))


if __name__ == "__main__":
    unittest.main()

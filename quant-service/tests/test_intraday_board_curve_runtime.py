from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import unittest

from app.intraday_board_curve_runtime import (
    IntradayBoardCurveRuntimeDependencies,
    run_intraday_board_curve_runtime_loop,
)


class _Result:
    def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))
        return _Result()


class _Database:
    def __init__(self):
        self.connection = _Connection()

    @contextmanager
    def transaction(self):
        yield self.connection


class IntradayBoardCurveRuntimeTests(unittest.TestCase):
    def test_runtime_keeps_source_scoped_curve_and_rotation_retention(self) -> None:
        database = _Database()
        calls = []
        observed_at = datetime(2026, 8, 22, 7, tzinfo=timezone.utc)

        async def run_database(operation, *args, **kwargs):
            calls.append(operation.__name__)
            return operation(*args)

        async def board_session():
            return True, "continuous_auction"

        async def storage_allowed():
            return True, {"state": "healthy"}

        async def capture():
            return {"status": "completed"}

        async def run_loop(**kwargs):
            await kwargs["prune_before"](observed_at, 60, 30)
            self.assertEqual(kwargs["curve_retention_days"](), 60)
            self.assertEqual(kwargs["rotation_retention_days"](), 30)

        asyncio.run(run_intraday_board_curve_runtime_loop(IntradayBoardCurveRuntimeDependencies(
            database=database, run_database=run_database, board_session=board_session,
            storage_allowed=storage_allowed, capture=capture, curve_retention_days=lambda: 60,
            rotation_retention_days=lambda: 30, run_loop=run_loop,
        )))
        self.assertEqual(calls, ["prune"])
        curve_query, curve_params = database.connection.executed[0]
        rotation_query, rotation_params = database.connection.executed[1]
        self.assertIn("intraday_board_flow_snapshots", curve_query)
        self.assertEqual(curve_params, (datetime(2026, 6, 23, 7, tzinfo=timezone.utc),))
        self.assertIn("intraday_board_rotation_events", rotation_query)
        self.assertEqual(rotation_params, (datetime(2026, 7, 23, 7, tzinfo=timezone.utc),))


if __name__ == "__main__":
    unittest.main()

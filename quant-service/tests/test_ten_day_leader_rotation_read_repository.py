from __future__ import annotations

from contextlib import asynccontextmanager
import unittest

from app.ten_day_leader_rotation_read_repository import latest_ten_day_leader_rotation


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return next(self.results)


class _Database:
    def __init__(self, results):
        self.connection = _Connection(results)

    @asynccontextmanager
    async def transaction(self):
        yield self.connection


class TenDayLeaderRotationReadRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_blocked_latest_run_separate_from_latest_intraday_pool(self) -> None:
        database = _Database([
            _Result(row={"run_id": "blocked", "status": "blocked", "as_of_date": "2026-08-21"}),
            _Result(rows=[]),
            _Result(row={"run_id": "completed", "status": "completed", "as_of_date": "2026-08-20"}),
            _Result(row={"scan_id": "scan-1", "observed_at": "2026-08-24T07:00:00Z", "observed_count": 20,
                         "shadow_eligible_count": 0, "decision_eligible_count": 0, "quote_sources": ["tencent_free"]}),
        ])

        result = await latest_ten_day_leader_rotation(database, limit=5)

        self.assertEqual(result["run"]["status"], "blocked")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["intraday"]["pool_run"]["run_id"], "completed")
        self.assertEqual(result["intraday"]["latest_batch"]["observed_count"], 20)
        self.assertIn("WHERE status IN ('completed','partial')", database.connection.calls[2][0])
        self.assertIn("shadow_eligible_count", database.connection.calls[3][0])


if __name__ == "__main__":
    unittest.main()

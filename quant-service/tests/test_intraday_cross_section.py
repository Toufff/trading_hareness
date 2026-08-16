from __future__ import annotations

import asyncio
import unittest

from app.intraday_cross_section import SharedAsyncSnapshot


class SharedAsyncSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_callers_share_one_request_then_use_received_age(self) -> None:
        calls = 0
        clock = [100.0]
        release = asyncio.Event()

        async def fetch() -> list[dict[str, int]]:
            nonlocal calls
            calls += 1
            await release.wait()
            return [{"rows": calls}]

        snapshot = SharedAsyncSnapshot(fetch, ttl_seconds=30.0, clock=lambda: clock[0])
        first = asyncio.create_task(snapshot.get())
        await asyncio.sleep(0)
        second = asyncio.create_task(snapshot.get())
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()
        (first_rows, first_status), (second_rows, second_status) = await asyncio.gather(first, second)
        self.assertEqual(first_rows, [{"rows": 1}])
        self.assertEqual(second_rows, [{"rows": 1}])
        self.assertEqual(first_status["status"], "fresh")
        self.assertEqual(second_status["status"], "fresh")
        clock[0] += 12.5
        rows, cached_status = await snapshot.get()
        self.assertEqual(rows, [{"rows": 1}])
        self.assertEqual(calls, 1)
        self.assertEqual(cached_status, {"status": "cached", "age_seconds": 12.5, "ttl_seconds": 30.0})

    async def test_failed_fetch_is_not_cached_and_next_call_retries(self) -> None:
        calls = 0

        async def fetch() -> list[int]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("upstream unavailable")
            return [2]

        snapshot = SharedAsyncSnapshot(fetch, ttl_seconds=30.0, clock=lambda: 1.0)
        with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
            await snapshot.get()
        rows, status = await snapshot.get()
        self.assertEqual(calls, 2)
        self.assertEqual(rows, [2])
        self.assertEqual(status["status"], "fresh")

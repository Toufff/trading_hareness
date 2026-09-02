from __future__ import annotations

import asyncio
import unittest

from app.post_close_refresh_runtime import PostCloseRefreshRuntime


class PostCloseRefreshRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_detaches_refresh_and_reports_active_task(self) -> None:
        runtime = PostCloseRefreshRuntime()
        started = asyncio.Event()
        release = asyncio.Event()

        async def refresh() -> dict[str, str]:
            started.set()
            await release.wait()
            return {"status": "completed"}

        accepted = runtime.start(refresh)
        self.assertEqual(accepted["status"], "running")
        self.assertFalse(accepted["already_running"])
        await started.wait()
        self.assertEqual(runtime.active_count, 1)

        duplicate = runtime.start(refresh)
        self.assertEqual(duplicate["status"], "running")
        self.assertTrue(duplicate["already_running"])

        release.set()
        for _ in range(10):
            if runtime.active_count == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(runtime.active_count, 0)

    async def test_refresh_continues_when_requesting_task_is_cancelled(self) -> None:
        runtime = PostCloseRefreshRuntime()
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def refresh() -> dict[str, str]:
            started.set()
            await release.wait()
            completed.set()
            return {"status": "completed"}

        request = asyncio.create_task(runtime.run(refresh))
        await started.wait()
        self.assertEqual(runtime.active_count, 1)
        self.assertEqual(runtime.status()["active_count"], 1)
        self.assertIsNotNone(runtime.status()["oldest_started_at"])

        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)
        for _ in range(10):
            if runtime.active_count == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(runtime.active_count, 0)
        self.assertEqual(
            runtime.status(),
            {"active_count": 0, "oldest_started_at": None, "last_outcome": {"status": "completed"}},
        )


class PostCloseRefreshRuntimeDoneCallbackTests(unittest.IsolatedAsyncioTestCase):
    """WP6: a detached ``start()`` task must always have its exception
    retrieved, and a 409 lease conflict must map to a clear terminal status
    rather than surfacing only as an unobserved-exception warning."""

    async def test_start_retrieves_a_409_lease_conflict_without_an_unobserved_exception(self) -> None:
        from fastapi import HTTPException

        runtime = PostCloseRefreshRuntime()

        async def refresh_conflicts() -> dict[str, str]:
            raise HTTPException(status_code=409, detail="a post-close refresh is already running")

        runtime.start(refresh_conflicts)
        for _ in range(20):
            if runtime.active_count == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(runtime.active_count, 0)
        self.assertEqual(
            runtime.status()["last_outcome"],
            {"status": "lease_conflict", "detail": "a post-close refresh is already running"},
        )

    async def test_start_logs_and_records_an_unexpected_failure(self) -> None:

        runtime = PostCloseRefreshRuntime()

        async def refresh_fails() -> dict[str, str]:
            raise RuntimeError("provider exploded")

        with self.assertLogs("app.post_close_refresh_runtime", level="ERROR") as captured:
            runtime.start(refresh_fails)
            for _ in range(20):
                if runtime.active_count == 0:
                    break
                await asyncio.sleep(0)
        self.assertEqual(runtime.active_count, 0)
        self.assertEqual(runtime.status()["last_outcome"]["status"], "failed")
        self.assertIn("provider exploded", runtime.status()["last_outcome"]["error"])
        self.assertTrue(any("provider exploded" in message for message in captured.output))

    async def test_run_still_propagates_the_exception_to_its_awaiter(self) -> None:
        """The done-callback change must not swallow the exception for run()."""
        runtime = PostCloseRefreshRuntime()

        async def refresh_fails() -> dict[str, str]:
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await runtime.run(refresh_fails)
        self.assertEqual(runtime.status()["last_outcome"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

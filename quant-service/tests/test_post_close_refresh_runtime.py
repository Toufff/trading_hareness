from __future__ import annotations

import asyncio
import unittest

from app.post_close_refresh_runtime import PostCloseRefreshRuntime


class PostCloseRefreshRuntimeTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()

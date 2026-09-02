"""Unit coverage for framework-free runtime composition wiring."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.runtime_composition import (
    LeasedRuntimeDependencies,
    build_leased_task_runner,
    current_lease_fence,
    lease_key_for_label,
)


class RuntimeCompositionTests(unittest.TestCase):
    def test_leased_runner_uses_one_stable_key_and_delegates_supervision(self):
        async def check() -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
            acquire = AsyncMock(return_value=True)
            renew = AsyncMock(return_value=True)
            release = AsyncMock()
            supervise = AsyncMock()
            database = object()
            holder = object()
            on_state = MagicMock()
            runner = build_leased_task_runner(LeasedRuntimeDependencies(
                database=database,
                lease_holder_id=holder,
                lease_seconds=45,
                acquire_lease=acquire,
                renew_lease=renew,
                release_lease=release,
                supervise=supervise,
                on_state=on_state,
            ))

            async def loop() -> None:
                return None

            await runner("intraday_monitor", loop)
            self.assertEqual(supervise.await_args.args[:2], ("intraday_monitor", loop))
            self.assertEqual(supervise.await_args.args[5], 45)
            self.assertIs(supervise.await_args.kwargs["on_state"], on_state)

            await supervise.await_args.args[2]()
            await supervise.await_args.args[3]()
            await supervise.await_args.args[4]()
            acquire.assert_awaited_once_with(database, "background_loop:intraday_monitor", holder, 45)
            renew.assert_awaited_once_with(database, "background_loop:intraday_monitor", holder, 45)
            release.assert_awaited_once_with(database, "background_loop:intraday_monitor", holder)
            return acquire, renew, release, supervise

        asyncio.run(check())

    def test_acquire_publishes_its_fence_for_the_task_body_to_read(self):
        async def check() -> None:
            acquire = AsyncMock(return_value=9)
            renew = AsyncMock(return_value=9)
            release = AsyncMock()
            supervise = AsyncMock()
            runner = build_leased_task_runner(LeasedRuntimeDependencies(
                database=object(), lease_holder_id=object(), lease_seconds=45,
                acquire_lease=acquire, renew_lease=renew, release_lease=release, supervise=supervise,
            ))

            async def loop() -> None:
                return None

            self.assertIsNone(current_lease_fence("fence_probe"))
            await runner("fence_probe", loop)
            await supervise.await_args.args[2]()  # invoke the wrapped acquire()
            self.assertEqual(current_lease_fence("fence_probe"), 9)
            self.assertEqual(lease_key_for_label("fence_probe"), "background_loop:fence_probe")

        asyncio.run(check())

    def test_acquire_publishes_none_when_the_lease_is_held_elsewhere(self):
        async def check() -> None:
            acquire = AsyncMock(return_value=None)
            supervise = AsyncMock()
            runner = build_leased_task_runner(LeasedRuntimeDependencies(
                database=object(), lease_holder_id=object(), lease_seconds=45,
                acquire_lease=acquire, renew_lease=AsyncMock(), release_lease=AsyncMock(), supervise=supervise,
            ))

            async def loop() -> None:
                return None

            await runner("fence_probe_lost", loop)
            owns_lease = await supervise.await_args.args[2]()
            self.assertFalse(owns_lease)
            self.assertIsNone(current_lease_fence("fence_probe_lost"))

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()

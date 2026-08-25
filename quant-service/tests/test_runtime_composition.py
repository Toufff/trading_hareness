"""Unit coverage for framework-free runtime composition wiring."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.runtime_composition import LeasedRuntimeDependencies, build_leased_task_runner


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


if __name__ == "__main__":
    unittest.main()

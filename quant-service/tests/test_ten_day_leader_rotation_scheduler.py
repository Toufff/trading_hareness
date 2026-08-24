from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.ten_day_leader_rotation_scheduler import (
    TenDayLeaderRotationSchedulerDependencies,
    ten_day_leader_rotation_scheduler_step,
)


class TenDayLeaderRotationSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_one_independent_same_date_shadow_materialization(self) -> None:
        local = datetime(2026, 8, 13, 19, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        dependencies = TenDayLeaderRotationSchedulerDependencies(
            calendar_open=AsyncMock(return_value=True), ready_window=lambda _: True,
            completed_for_date=AsyncMock(return_value=False), run=AsyncMock(return_value="completed"),
            now=lambda: local,
        )
        completed_dates = set()

        completed = await ten_day_leader_rotation_scheduler_step(completed_dates, dependencies, local=local)

        self.assertTrue(completed)
        self.assertEqual(completed_dates, {local.date()})
        dependencies.run.assert_awaited_once_with(local.date())

    async def test_blocked_attempt_remains_retryable(self) -> None:
        local = datetime(2026, 8, 13, 19, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        dependencies = TenDayLeaderRotationSchedulerDependencies(
            calendar_open=AsyncMock(return_value=True), ready_window=lambda _: True,
            completed_for_date=AsyncMock(return_value=False), run=AsyncMock(return_value="blocked"),
            now=lambda: local,
        )
        completed_dates = set()

        self.assertFalse(await ten_day_leader_rotation_scheduler_step(completed_dates, dependencies, local=local))
        self.assertEqual(completed_dates, set())


if __name__ == "__main__":
    unittest.main()

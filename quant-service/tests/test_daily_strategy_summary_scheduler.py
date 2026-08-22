from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.daily_strategy_summary_scheduler import (
    DailyStrategySummarySchedulerDependencies,
    daily_strategy_summary_scheduler_step,
)


CN = ZoneInfo("Asia/Shanghai")


class DailyStrategySummarySchedulerTests(unittest.IsolatedAsyncioTestCase):
    def _dependencies(self, *, terminal: bool = False, status: str = "suppressed"):
        calls: list[str] = []

        async def calendar_open(_day):
            calls.append("calendar")
            return True

        async def terminal_for_date(_day):
            calls.append("terminal")
            return terminal

        async def run_summary(_day):
            calls.append("run")
            return {"status": status}

        return DailyStrategySummarySchedulerDependencies(
            calendar_open=calendar_open, terminal_for_date=terminal_for_date,
            run_summary=run_summary, now=lambda: datetime(2026, 8, 14, 19, 15, tzinfo=CN),
            report_error=lambda _message: None,
        ), calls

    async def test_restart_reuses_terminal_summary_without_rebuilding(self):
        dependencies, calls = self._dependencies(terminal=True)
        completed = set()
        done = await daily_strategy_summary_scheduler_step(completed, dependencies)
        self.assertTrue(done)
        self.assertEqual(calls, ["calendar", "terminal"])
        self.assertIn(datetime(2026, 8, 14, tzinfo=CN).date(), completed)

    async def test_failed_summary_remains_retryable(self):
        dependencies, calls = self._dependencies(status="failed")
        completed = set()
        done = await daily_strategy_summary_scheduler_step(completed, dependencies)
        self.assertFalse(done)
        self.assertEqual(calls, ["calendar", "terminal", "run"])
        self.assertEqual(completed, set())

    async def test_outside_window_does_not_touch_database_callbacks(self):
        dependencies, calls = self._dependencies()
        done = await daily_strategy_summary_scheduler_step(
            set(), dependencies, local=datetime(2026, 8, 14, 19, 30, tzinfo=CN),
        )
        self.assertFalse(done)
        self.assertEqual(calls, [])

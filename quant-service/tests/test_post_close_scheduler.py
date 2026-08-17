from __future__ import annotations

from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from app.post_close_scheduler import PostCloseSchedulerDependencies, post_close_scheduler_step


CN = ZoneInfo("Asia/Shanghai")


class PostCloseSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def _dependencies(self, *, retry_window=lambda _local: True, complete=(False, False),
                      strategy_status="completed", main_wave_status="completed"):
        calls: list[tuple[str, object]] = []

        async def calendar_open(day):
            calls.append(("calendar", day))
            return True

        async def completed_for_date(day):
            calls.append(("completed", day))
            return complete

        async def run_strategy(day):
            calls.append(("strategy", day))
            return strategy_status

        async def run_main_wave(day):
            calls.append(("main_wave", day))
            return main_wave_status

        dependencies = PostCloseSchedulerDependencies(
            calendar_open=calendar_open,
            retry_window=retry_window,
            completed_for_date=completed_for_date,
            run_strategy=run_strategy,
            run_main_wave=run_main_wave,
            now=lambda: datetime(2026, 8, 14, 19, 0, tzinfo=CN),
            report_error=lambda _message: None,
        )
        return dependencies, calls

    async def test_step_does_not_touch_database_callbacks_outside_retry_window(self):
        dependencies, calls = self._dependencies(retry_window=lambda _local: False)

        completed = await post_close_scheduler_step(set(), dependencies)

        self.assertFalse(completed)
        self.assertEqual(calls, [])

    async def test_step_requires_both_same_date_outputs_before_completion(self):
        dependencies, calls = self._dependencies(complete=(False, False))
        completed_dates = set()

        completed = await post_close_scheduler_step(completed_dates, dependencies)

        self.assertTrue(completed)
        self.assertEqual(completed_dates, {datetime(2026, 8, 14, tzinfo=CN).date()})
        self.assertEqual([item[0] for item in calls], ["calendar", "completed", "strategy", "main_wave"])
        self.assertTrue(all(item[1] == datetime(2026, 8, 14, tzinfo=CN).date() for item in calls))

    async def test_partial_or_failed_main_wave_does_not_mark_exchange_date_complete(self):
        dependencies, calls = self._dependencies(complete=(False, False), main_wave_status="blocked")
        completed_dates = set()

        completed = await post_close_scheduler_step(completed_dates, dependencies)

        self.assertFalse(completed)
        self.assertEqual(completed_dates, set())
        self.assertEqual([item[0] for item in calls], ["calendar", "completed", "strategy", "main_wave"])

    async def test_existing_same_date_completion_avoids_reexecution(self):
        dependencies, calls = self._dependencies(complete=(True, True))
        completed_dates = set()

        completed = await post_close_scheduler_step(completed_dates, dependencies)

        self.assertTrue(completed)
        self.assertEqual([item[0] for item in calls], ["calendar", "completed"])


if __name__ == "__main__":
    unittest.main()

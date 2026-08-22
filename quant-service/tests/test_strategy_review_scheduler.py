from __future__ import annotations

from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from app.strategy_review_scheduler import StrategyReviewSchedulerDependencies, strategy_review_scheduler_step


CN = ZoneInfo("Asia/Shanghai")


class StrategyReviewSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def _dependencies(self, *, calendar_is_open=True, fail_operation: str | None = None):
        calls: list[tuple[str, object]] = []

        async def calendar_open(day):
            calls.append(("calendar", day))
            return calendar_is_open

        async def operation(name, *args):
            calls.append((name, args))
            if name == fail_operation:
                raise RuntimeError(name)
            return {"status": "completed"}

        dependencies = StrategyReviewSchedulerDependencies(
            calendar_open=calendar_open,
            sync_index_context=lambda day: operation("index", day),
            build_market_snapshot=lambda day, session: operation("snapshot", day, session),
            build_board_report=lambda: operation("board"),
            recompute_outcomes=lambda day: operation("outcomes", day),
            recompute_analyst_intraday_outcomes=lambda day: operation("analyst_intraday_outcomes", day),
            recompute_scorecards=lambda day: operation("scorecards", day),
            persist_review=lambda day, session: operation("persist", day, session),
            now=lambda: datetime(2026, 8, 14, 11, 31, tzinfo=CN),
            report_error=lambda _message: None,
        )
        return dependencies, calls

    async def test_step_does_nothing_when_exchange_is_closed(self):
        dependencies, calls = self._dependencies(calendar_is_open=False)

        result = await strategy_review_scheduler_step(set(), dependencies)

        self.assertEqual(result, ())
        self.assertEqual([name for name, _ in calls], ["calendar"])

    async def test_midday_only_builds_snapshot_board_and_review(self):
        dependencies, calls = self._dependencies()
        completed: set[tuple[object, str]] = set()

        result = await strategy_review_scheduler_step(
            completed, dependencies, local=datetime(2026, 8, 14, 11, 31, 30, tzinfo=CN),
        )

        self.assertEqual(result, ("midday",))
        self.assertEqual([name for name, _ in calls], ["calendar", "snapshot", "board", "persist"])
        self.assertIn((datetime(2026, 8, 14, tzinfo=CN).date(), "midday"), completed)

    async def test_close_runs_index_settlement_and_scorecards_before_review(self):
        dependencies, calls = self._dependencies()
        completed: set[tuple[object, str]] = set()

        result = await strategy_review_scheduler_step(
            completed, dependencies, local=datetime(2026, 8, 14, 15, 5, 30, tzinfo=CN),
        )

        self.assertEqual(result, ("close",))
        self.assertEqual(
            [name for name, _ in calls],
            ["calendar", "index", "snapshot", "board", "outcomes", "analyst_intraday_outcomes", "scorecards", "persist"],
        )
        self.assertIn((datetime(2026, 8, 14, tzinfo=CN).date(), "close"), completed)

    async def test_failed_checkpoint_is_not_completed_and_can_retry(self):
        dependencies, calls = self._dependencies(fail_operation="board")
        completed: set[tuple[object, str]] = set()

        result = await strategy_review_scheduler_step(
            completed, dependencies, local=datetime(2026, 8, 14, 11, 31, tzinfo=CN),
        )

        self.assertEqual(result, ())
        self.assertEqual(completed, set())
        self.assertEqual([name for name, _ in calls], ["calendar", "snapshot", "board"])

    async def test_restart_reuses_completed_checkpoint_without_repeating_side_effects(self):
        dependencies, calls = self._dependencies()
        receipt_calls: list[tuple[object, str]] = []

        async def completed_for_checkpoint(day, session):
            receipt_calls.append((day, session))
            return session == "midday"

        dependencies = dependencies.__class__(**{
            **dependencies.__dict__, "completed_for_checkpoint": completed_for_checkpoint,
        })
        completed: set[tuple[object, str]] = set()
        result = await strategy_review_scheduler_step(
            completed, dependencies, local=datetime(2026, 8, 14, 11, 31, 30, tzinfo=CN),
        )
        self.assertEqual(result, ("midday",))
        self.assertEqual(receipt_calls, [(datetime(2026, 8, 14, tzinfo=CN).date(), "midday")])
        self.assertEqual([name for name, _ in calls], ["calendar"])
        self.assertIn((datetime(2026, 8, 14, tzinfo=CN).date(), "midday"), completed)

    async def test_close_generates_daily_and_friday_weekly_analyst_reviews(self):
        dependencies, calls = self._dependencies()
        analyst_reviews: list[tuple[str, object]] = []

        async def build_review(cadence, day):
            analyst_reviews.append((cadence, day))

        dependencies = dependencies.__class__(**{
            **dependencies.__dict__, "build_analyst_market_review": build_review,
        })
        completed: set[tuple[object, str]] = set()
        await strategy_review_scheduler_step(
            completed, dependencies, local=datetime(2026, 8, 14, 15, 5, 30, tzinfo=CN),
        )
        self.assertEqual([cadence for cadence, _ in analyst_reviews], ["daily", "weekly"])


if __name__ == "__main__":
    unittest.main()

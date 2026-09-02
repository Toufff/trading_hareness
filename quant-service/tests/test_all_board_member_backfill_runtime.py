import asyncio
from datetime import datetime, timezone
import unittest

from app.all_board_member_backfill_runtime import (
    AllBoardMemberBackfillLoopDependencies,
    all_board_member_backfill_loop,
)

#: 16:00 Asia/Shanghai -- inside the 15:10-18:00 post-close backfill window.
_WITHIN_WINDOW = lambda: datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)  # noqa: E731
#: 10:00 Asia/Shanghai -- outside the post-close backfill window.
_OUTSIDE_WINDOW = lambda: datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)  # noqa: E731


class _StopLoop(Exception):
    """Breaks the loop's ``while True`` after a bounded number of ticks."""


class AllBoardMemberBackfillRuntimeTests(unittest.TestCase):
    def test_runs_a_batch_and_sleeps_ninety_seconds_when_open(self):
        sleeps: list[float] = []

        async def sse_calendar_open_async(_date):
            return True

        batch_calls = []

        async def run_batch():
            batch_calls.append(1)

        def log_failure(_message):
            raise AssertionError("must not log a failure for a successful batch")

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise _StopLoop()

        with self.assertRaises(_StopLoop):
            asyncio.run(all_board_member_backfill_loop(AllBoardMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_WITHIN_WINDOW,
            )))
        self.assertEqual(sleeps, [90, 90])
        self.assertEqual(len(batch_calls), 2)

    def test_sleeps_sixty_seconds_outside_the_post_close_window_without_running_a_batch(self):
        sleeps: list[float] = []

        async def sse_calendar_open_async(_date):
            return True

        async def run_batch():
            raise AssertionError("must not run a batch outside the post-close window")

        def log_failure(_message):
            raise AssertionError("must not log a failure when no batch ran")

        async def sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop()

        with self.assertRaises(_StopLoop):
            asyncio.run(all_board_member_backfill_loop(AllBoardMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_OUTSIDE_WINDOW,
            )))
        self.assertEqual(sleeps, [60])

    def test_sleeps_sixty_seconds_when_calendar_is_closed_without_running_a_batch(self):
        sleeps: list[float] = []

        async def sse_calendar_open_async(_date):
            return False

        async def run_batch():
            raise AssertionError("must not run a batch when the calendar is closed")

        def log_failure(_message):
            raise AssertionError("must not log a failure when no batch ran")

        async def sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop()

        with self.assertRaises(_StopLoop):
            asyncio.run(all_board_member_backfill_loop(AllBoardMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_WITHIN_WINDOW,
            )))
        self.assertEqual(sleeps, [60])

    def test_a_batch_failure_is_logged_and_the_loop_continues(self):
        sleeps: list[float] = []
        failures: list[str] = []

        async def sse_calendar_open_async(_date):
            return True

        async def run_batch():
            raise RuntimeError("boom")

        def log_failure(message):
            failures.append(message)

        async def sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop()

        with self.assertRaises(_StopLoop):
            asyncio.run(all_board_member_backfill_loop(AllBoardMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_WITHIN_WINDOW,
            )))
        self.assertEqual(failures, ["boom"])
        self.assertEqual(sleeps, [90])


if __name__ == "__main__":
    unittest.main()

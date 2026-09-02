import asyncio
from datetime import datetime, timezone
import unittest

from app.ths_concept_member_backfill_runtime import (
    ThsConceptMemberBackfillLoopDependencies,
    ths_concept_member_backfill_loop,
)

#: 16:00 Asia/Shanghai -- inside the 15:10-18:00 post-close backfill window.
_WITHIN_WINDOW = lambda: datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)  # noqa: E731
#: 10:00 Asia/Shanghai -- outside the post-close backfill window.
_OUTSIDE_WINDOW = lambda: datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)  # noqa: E731


class _StopLoop(Exception):
    """Breaks the loop's ``while True`` after a bounded number of ticks."""


class ThsConceptMemberBackfillRuntimeTests(unittest.TestCase):
    def test_runs_a_batch_and_sleeps_sixty_five_seconds_when_open(self):
        sleeps: list[float] = []
        batch_calls = []

        async def sse_calendar_open_async(_date):
            return True

        async def run_batch():
            batch_calls.append(1)

        def log_failure(_message):
            raise AssertionError("must not log a failure for a successful batch")

        async def sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop()

        with self.assertRaises(_StopLoop):
            asyncio.run(ths_concept_member_backfill_loop(ThsConceptMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_WITHIN_WINDOW,
            )))
        self.assertEqual(sleeps, [65])
        self.assertEqual(len(batch_calls), 1)

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
            asyncio.run(ths_concept_member_backfill_loop(ThsConceptMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_OUTSIDE_WINDOW,
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
            asyncio.run(ths_concept_member_backfill_loop(ThsConceptMemberBackfillLoopDependencies(
                sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch,
                log_failure=log_failure, sleep=sleep, now=_WITHIN_WINDOW,
            )))
        self.assertEqual(failures, ["boom"])
        self.assertEqual(sleeps, [65])


if __name__ == "__main__":
    unittest.main()

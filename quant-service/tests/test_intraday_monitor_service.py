"""Regression coverage for what the intraday monitor loop reports when a pass fails.

The loop deliberately swallows a failing source pass so a later interval can
recover.  What it logs is therefore the only record that the failure happened,
and on 2026-08-28 that record was not enough to act on: 352 passes - about 68%
of the session - died with a bare "Object of type Decimal is not JSON
serializable" naming no file, no line and no source pass, and the failure did
not reproduce off the edge.
"""

from __future__ import annotations

import asyncio
import unittest


class _Stop(BaseException):
    """Escape the endless loop from outside its ``except Exception`` guard."""


def _run_one_pass(*, scan_result, board_result=None):
    """Drive exactly one monitor iteration and return everything it logged."""
    from app.intraday_monitor_service import run_intraday_monitor_loop

    logged: list[str] = []
    delays = {"calls": 0}

    def next_delay_seconds(_interval, _local):
        delays["calls"] += 1
        # Called once per iteration before the guarded block; the second call
        # ends the loop without passing through its exception handling.
        if delays["calls"] > 1:
            raise _Stop
        return 0.0

    async def realtime_session():
        return True, "open"

    async def scan_watchlist(_request):
        if isinstance(scan_result, Exception):
            raise scan_result
        return scan_result

    async def run_board_report(**_kwargs):
        if isinstance(board_result, Exception):
            raise board_result
        return board_result or {"status": "completed"}

    async def drive():
        try:
            await run_intraday_monitor_loop(
                1,
                realtime_session=realtime_session,
                high_frequency_window=lambda _local: True,
                next_delay_seconds=next_delay_seconds,
                make_scan_request=lambda limit, offset: {"limit": limit, "offset": offset},
                scan_watchlist=scan_watchlist,
                board_refresh_interval_seconds=lambda _local: 60.0,
                run_board_report=run_board_report,
                log=logged.append,
            )
        except _Stop:
            pass

    asyncio.run(drive())
    return logged


def _raise_decimal_error() -> Exception:
    """Build a real raised exception so it carries frames, not a bare instance."""
    from decimal import Decimal
    import json

    try:
        json.dumps({"strength": Decimal("0.78")})
    except TypeError as error:  # pragma: no cover - the raise is the point
        return error
    raise AssertionError("expected a TypeError from the Decimal payload")


class FailedSourcePassIsLocatableTests(unittest.TestCase):
    def test_the_log_carries_the_frames_not_only_the_message(self):
        logged = _run_one_pass(scan_result=_raise_decimal_error())
        failures = [line for line in logged if "source pass failed" in line]
        self.assertEqual(len(failures), 1)
        self.assertIn("Object of type Decimal is not JSON serializable", failures[0])
        self.assertIn("Traceback", failures[0],
                      "a message with no frames is what made 2026-08-28 unaddressable")
        self.assertIn("json", failures[0])

    def test_a_successful_pass_logs_nothing(self):
        logged = _run_one_pass(scan_result={"realtime_validation": {"next_offset": 8}})
        self.assertEqual([line for line in logged if "source pass failed" in line], [])

    def test_a_failing_pass_does_not_end_the_loop(self):
        # The loop must survive to the next interval; it is stopped here only
        # by the sentinel raised outside its guarded block.
        logged = _run_one_pass(scan_result=RuntimeError("upstream unavailable"))
        self.assertTrue(any("upstream unavailable" in line for line in logged))

    def test_each_failing_job_is_reported_separately(self):
        logged = _run_one_pass(scan_result=RuntimeError("scan down"),
                               board_result=RuntimeError("board down"))
        failures = [line for line in logged if "source pass failed" in line]
        self.assertEqual(len(failures), 2)
        self.assertTrue(any("scan down" in line for line in failures))
        self.assertTrue(any("board down" in line for line in failures))

    def test_the_frame_tail_is_bounded(self):
        logged = _run_one_pass(scan_result=_raise_decimal_error())
        failure = next(line for line in logged if "source pass failed" in line)
        # Bounded so one repeating failure cannot flood the journal, but long
        # enough to hold the frames that name the defect.
        self.assertLess(len(failure), 2200)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.intraday_minute_profile_runner import run_iteration


class IntradayMinuteProfileRunnerTests(unittest.TestCase):
    def test_outside_close_window_does_not_touch_calendar_or_provider(self) -> None:
        async def forbidden(*_args):
            raise AssertionError("outside the close window must remain local")

        completed = asyncio.run(run_iteration(
            set(), calendar_open=forbidden, load_symbols=forbidden, storage_allowed=forbidden, capture=forbidden,
            now_utc=lambda: datetime(2026, 8, 21, 1, tzinfo=timezone.utc),  # 09:00 Shanghai
        ))
        self.assertEqual(completed, set())

    def test_completed_capture_is_recorded_once_for_the_exchange_day(self) -> None:
        now = datetime(2026, 8, 21, 6, 57, tzinfo=timezone.utc)  # 14:57 Shanghai

        async def check():
            calls: list[object] = []

            async def calendar_open(value):
                calls.append(("calendar", value))
                return True

            async def load_symbols():
                calls.append("load")
                return ["000001.SZ"]

            async def storage_allowed():
                calls.append("storage")
                return True, {"state": "ok"}

            async def capture(symbols):
                calls.append(("capture", symbols))
                return {"status": "completed"}

            state = await run_iteration(
                set(), calendar_open=calendar_open, load_symbols=load_symbols,
                storage_allowed=storage_allowed, capture=capture, now_utc=lambda: now,
            )
            repeat = await run_iteration(
                state, calendar_open=calendar_open, load_symbols=load_symbols,
                storage_allowed=storage_allowed, capture=capture, now_utc=lambda: now,
            )
            return state, repeat, calls

        state, repeat, calls = asyncio.run(check())
        self.assertEqual(len(state), 1)
        self.assertEqual(repeat, state)
        self.assertEqual(calls, [("calendar", next(iter(state))), "load", "storage", ("capture", ["000001.SZ"])])

    def test_transient_capture_failure_remains_eligible_for_retry(self) -> None:
        now = datetime(2026, 8, 21, 6, 57, tzinfo=timezone.utc)

        async def calendar_open(_value):
            return True

        async def load_symbols():
            return ["000001.SZ"]

        async def storage_allowed():
            return True, {"state": "ok"}

        async def capture(_symbols):
            raise RuntimeError("temporary source error")

        completed = asyncio.run(run_iteration(
            set(), calendar_open=calendar_open, load_symbols=load_symbols,
            storage_allowed=storage_allowed, capture=capture, now_utc=lambda: now, emit=lambda _message: None,
        ))
        self.assertEqual(completed, set())

    def test_runner_has_no_main_or_http_client_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_minute_profile_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("time(14, 55)", source)


if __name__ == "__main__":
    unittest.main()

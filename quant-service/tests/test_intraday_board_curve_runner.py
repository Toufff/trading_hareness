from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.intraday_board_curve_runner import next_delay, run_iteration


class IntradayBoardCurveRunnerTests(unittest.TestCase):
    def test_delay_is_bounded_to_next_minute_boundary(self) -> None:
        local = datetime(2026, 8, 21, 10, 0, 45, tzinfo=timezone.utc)
        self.assertEqual(next_delay(local), 16.0)
        self.assertEqual(next_delay(local.replace(second=0)), 30.0)

    def test_one_active_minute_prunes_once_and_never_replays_after_failure(self) -> None:
        now = datetime(2026, 8, 21, 2, 0, 45, tzinfo=timezone.utc)  # 10:00:45 Shanghai

        async def check():
            calls: list[object] = []

            async def board_session():
                return True, "open"

            async def prune_before(*args):
                calls.append(("prune", args))

            async def storage_allowed():
                calls.append("storage")
                return True, {"state": "ok"}

            async def capture():
                calls.append("capture")
                raise RuntimeError("temporary upstream error")

            first = await run_iteration(
                None, None, board_session=board_session, prune_before=prune_before,
                storage_allowed=storage_allowed, capture=capture,
                curve_retention_days=lambda: 7, rotation_retention_days=lambda: 3,
                now_utc=lambda: now, emit=lambda _message: None,
            )
            second = await run_iteration(
                first[0], first[1], board_session=board_session, prune_before=prune_before,
                storage_allowed=storage_allowed, capture=capture,
                curve_retention_days=lambda: 7, rotation_retention_days=lambda: 3,
                now_utc=lambda: now, emit=lambda _message: None,
            )
            return first, second, calls

        first, second, calls = asyncio.run(check())
        self.assertEqual(first[0].strftime("%H:%M"), "10:00")
        self.assertEqual(first[1].isoformat(), "2026-08-21")
        self.assertEqual(first[2], 16.0)
        self.assertEqual(second, first)
        self.assertEqual(calls, [("prune", (now, 7, 3)), "storage", "capture"])

    def test_inactive_session_does_not_prune_or_capture(self) -> None:
        async def check():
            async def inactive():
                return False, "closed"

            async def forbidden(*_args):
                raise AssertionError("inactive board session must stay local")

            return await run_iteration(
                None, None, board_session=inactive, prune_before=forbidden, storage_allowed=forbidden, capture=forbidden,
                curve_retention_days=lambda: 7, rotation_retention_days=lambda: 3,
                now_utc=lambda: datetime(2026, 8, 21, 2, tzinfo=timezone.utc),
            )

        completed, pruned_on, _ = asyncio.run(check())
        self.assertIsNone(completed)
        self.assertIsNone(pruned_on)

    def test_runner_has_no_main_or_http_client_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_board_curve_runner.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("missed minutes are never replayed", source)


if __name__ == "__main__":
    unittest.main()

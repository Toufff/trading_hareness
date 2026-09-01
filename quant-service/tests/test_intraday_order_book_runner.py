from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.intraday_order_book_runner import run_iteration


class IntradayOrderBookRunnerTests(unittest.TestCase):
    def test_open_circuit_backs_off_without_loading_or_capturing(self) -> None:
        async def check():
            calls: list[str] = []

            async def realtime_session():
                return True, "open"

            async def open_capabilities(*_args):
                return {"order_book_quote"}

            async def forbidden(*_args):
                calls.append("forbidden")
                raise AssertionError("must not run while circuit is open")

            return await run_iteration(
                None, realtime_session=realtime_session, open_capabilities=open_capabilities,
                load_symbols=forbidden, prune_before=forbidden, storage_allowed=forbidden, capture=forbidden,
                interval_seconds=lambda: 3.0, retention_days=lambda: 7,
            ), calls

        (pruned_on, delay), calls = asyncio.run(check())
        self.assertIsNone(pruned_on)
        self.assertEqual(delay, 15.0)
        self.assertEqual(calls, [])

    def test_active_iteration_prunes_once_then_captures_explicit_symbols(self) -> None:
        now = datetime(2026, 8, 21, 2, tzinfo=timezone.utc)

        async def check():
            observed: list[object] = []

            async def realtime_session():
                return True, "open"

            async def open_capabilities(*_args):
                return set()

            async def load_symbols():
                return ["000001.SZ", "600000.SH"]

            async def prune_before(when, days):
                observed.append(("prune", when, days))

            async def storage_allowed():
                return True, {"state": "ok"}

            async def capture(symbols):
                observed.append(("capture", symbols))
                return {"status": "completed"}

            first = await run_iteration(
                None, realtime_session=realtime_session, open_capabilities=open_capabilities,
                load_symbols=load_symbols, prune_before=prune_before, storage_allowed=storage_allowed,
                capture=capture, interval_seconds=lambda: 3.0, retention_days=lambda: 7, now_utc=lambda: now,
            )
            second = await run_iteration(
                first[0], realtime_session=realtime_session, open_capabilities=open_capabilities,
                load_symbols=load_symbols, prune_before=prune_before, storage_allowed=storage_allowed,
                capture=capture, interval_seconds=lambda: 3.0, retention_days=lambda: 7, now_utc=lambda: now,
            )
            return first, second, observed

        first, second, observed = asyncio.run(check())
        self.assertEqual(first, (date(2026, 8, 21), 3.0))
        self.assertEqual(second, (date(2026, 8, 21), 3.0))
        self.assertEqual(observed, [
            ("prune", now, 7), ("capture", ["000001.SZ", "600000.SH"]),
            ("capture", ["000001.SZ", "600000.SH"]),
        ])

    def test_runner_has_no_main_or_http_client_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_order_book_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("max(15.0, interval)", source)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.intraday_fast_quote_capture_service import capture


class IntradayFastQuoteCaptureServiceTests(unittest.TestCase):
    def test_valid_quote_persists_one_cross_check_with_calculated_change(self) -> None:
        async def check():
            persisted: list[tuple[object, ...]] = []

            async def provider(*_args):
                return SimpleNamespace(
                    rows=[{"ts_code": "000001.SZ", "close": 11.0, "pre_close": 10.0}],
                    provider=SimpleNamespace(key="tushare_super_get"),
                )

            async def run_database(operation, *args):
                persisted.append((operation, *args))

            return await capture(
                "000001.SZ", call_provider=provider, run_database=run_database,
                persist_quote=lambda *_args: None, persist_failure=lambda *_args: None,
                number=lambda value: float(value) if value is not None else None,
                safe_error=lambda value, _limit: value, is_circuit_open=lambda _error: False,
                now_utc=lambda: datetime(2026, 8, 21, 2, tzinfo=timezone.utc),
            ), persisted

        result, persisted = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["price"], 11.0)
        self.assertEqual(len(persisted), 1)
        self.assertAlmostEqual(persisted[0][4], 10.0)

    def test_circuit_open_is_not_persisted_as_a_provider_failure(self) -> None:
        class CircuitOpen(Exception):
            pass

        async def check():
            async def provider(*_args):
                raise CircuitOpen("circuit open")

            async def forbidden(*_args):
                raise AssertionError("circuit open must not write a failure")

            return await capture(
                "000001.SZ", call_provider=provider, run_database=forbidden,
                persist_quote=lambda *_args: None, persist_failure=lambda *_args: None,
                number=lambda value: value, safe_error=lambda value, _limit: value,
                is_circuit_open=lambda error: isinstance(error, CircuitOpen),
            )

        self.assertEqual(asyncio.run(check())["status"], "circuit_open")

    def test_service_has_no_main_or_http_client_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_fast_quote_capture_service.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("A provider circuit-open response", source)


if __name__ == "__main__":
    unittest.main()

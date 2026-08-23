from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from app.intraday_order_book_service import capture_snapshot, enabled, interval_seconds, max_symbols, retention_days


class IntradayOrderBookServiceTests(unittest.TestCase):
    def test_environment_bounds_are_explicit_and_safe(self) -> None:
        self.assertFalse(enabled({"INTRADAY_ORDER_BOOK_ENABLED": "off"}))
        self.assertTrue(enabled({"INTRADAY_ORDER_BOOK_ENABLED": "yes"}))
        self.assertEqual(interval_seconds({"INTRADAY_ORDER_BOOK_INTERVAL_SECONDS": "0.2"}), 3.0)
        self.assertEqual(interval_seconds({"INTRADAY_ORDER_BOOK_INTERVAL_SECONDS": "bad"}), 3.0)
        self.assertEqual(retention_days({"INTRADAY_ORDER_BOOK_RETENTION_DAYS": "100"}), 30)
        self.assertEqual(max_symbols({"INTRADAY_ORDER_BOOK_MAX_SYMBOLS": "1000"}), 80)

    def test_capture_filters_symbols_and_persists_one_bounded_batch(self) -> None:
        async def check() -> tuple[dict[str, object], list[object]]:
            observed: list[object] = []

            async def fetch(symbols, *, max_symbols):
                observed.extend([symbols, max_symbols])
                return [{"ts_code": symbols[0], "price": 10}]

            def persist(*args):
                observed.append(args)
                return 1

            def persist_error(*args):
                raise AssertionError(f"unexpected error persistence: {args}")

            async def run_database(operation, *args):
                return operation(*args)

            result = await capture_snapshot(
                ["000001.sz", "invalid", "000001.SZ", "600000.SH"], max_symbols_value=1,
                fetch_quotes=fetch, persist=persist, persist_error=persist_error,
                run_database=run_database, safe_error=lambda message, _limit: message,
                handled_errors=(RuntimeError,),
            )
            return result, observed

        result, observed = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["stored"], 1)
        self.assertEqual(observed[0], ["000001.SZ"])
        self.assertEqual(observed[1], 1)

    def test_capture_returns_auditable_failure_without_raising(self) -> None:
        async def check() -> tuple[dict[str, object], list[object]]:
            persisted: list[object] = []

            async def fetch(*_args, **_kwargs):
                raise RuntimeError("provider timeout")

            async def run_database(operation, *args):
                operation(*args)

            result = await capture_snapshot(
                ["000001.SZ"], max_symbols_value=40, fetch_quotes=fetch,
                persist=lambda *_args: 0, persist_error=lambda *args: persisted.append(args),
                run_database=run_database, safe_error=lambda message, _limit: f"safe:{message}",
                handled_errors=(RuntimeError,),
            )
            return result, persisted

        result, persisted = asyncio.run(check())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "safe:provider timeout")
        self.assertEqual(len(persisted), 1)

    def test_service_owns_no_fastapi_or_provider_client(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_order_book_service.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("def persist_observations", source)


if __name__ == "__main__":
    unittest.main()

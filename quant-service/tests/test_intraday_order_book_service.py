from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.intraday_order_book_service import capture_snapshot, enabled, interval_seconds, max_symbols, persist_observations, retention_days


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
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_order_book_service.py").read_text(encoding="utf-8")
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("def persist_observations", source)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class BatchedPersistenceSqlIntegrationTests(unittest.TestCase):
    """The per-row INSERT loop was replaced with one batched multi-row INSERT."""

    symbols = ["999992.SZ", "999991.SZ"]

    def _cleanup(self) -> None:
        from app.main import db
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.intraday_quote_observations WHERE symbol=ANY(%s)", (self.symbols,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (self.symbols,))

    def test_batched_insert_dedupes_and_reports_an_accurate_stored_count(self) -> None:
        from app.main import db, record_provider_success, strategy_json_safe
        self._cleanup()
        observed_at = datetime(2099, 1, 2, 1, 30, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                for symbol in self.symbols:
                    connection.execute(
                        "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT DO NOTHING", (symbol,),
                    )
            rows = [{"ts_code": symbol, "price": 10.0 + index, "pre_close": 10.0, "bid1": 10.0, "ask1": 10.1}
                    for index, symbol in enumerate(self.symbols)]
            stored_first = persist_observations(
                db, observed_at, rows, 50, json_safe=strategy_json_safe, record_success=record_provider_success,
            )
            self.assertEqual(stored_first, len(self.symbols))
            with db.transaction() as connection:
                persisted = connection.execute(
                    "SELECT symbol,price FROM quant.intraday_quote_observations WHERE symbol=ANY(%s) ORDER BY symbol",
                    (self.symbols,),
                ).fetchall()
            self.assertEqual({row["symbol"] for row in persisted}, set(self.symbols))
            # A second call at the exact same observed_at collides with the unique
            # (symbol,source_name,observed_at) index for both rows; the batched
            # INSERT's rowcount must reflect that nothing new was actually stored.
            stored_second = persist_observations(
                db, observed_at, rows, 50, json_safe=strategy_json_safe, record_success=record_provider_success,
            )
            self.assertEqual(stored_second, 0)
            # A later observed_at must be free to insert again (no dedup false positive).
            stored_third = persist_observations(
                db, observed_at + timedelta(seconds=30), rows, 50,
                json_safe=strategy_json_safe, record_success=record_provider_success,
            )
            self.assertEqual(stored_third, len(self.symbols))
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()

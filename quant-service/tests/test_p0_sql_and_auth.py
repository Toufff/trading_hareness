"""Regression coverage for P0 data guards that mocks cannot prove.

The SQL case intentionally runs only where a PostgreSQL service is configured
(the compose test container).  It writes one reserved future-date test row and
removes every row it created in ``finally``.
"""

from __future__ import annotations

import os
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import DailyBar, app, db, executor_saturated_response, upsert_bar
from app.runtime_executors import ExecutorSaturatedError


class WriteAuthenticationMiddlewareTests(unittest.TestCase):
    def test_daily_bar_repository_has_no_http_or_main_orchestration_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "daily_bar_repository.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("def upsert_daily_bar", source)

    def test_asgi_middleware_rejects_unsigned_writes_and_allows_valid_key(self) -> None:
        # Exercise the mounted production application and an actual protected
        # route.  Startup is intentionally no-op here: the request body is
        # invalid, so a valid key can prove middleware passage via HTTP 422
        # without touching PostgreSQL or starting background market loops.
        @asynccontextmanager
        async def no_lifespan(_: object):
            yield

        original_lifespan = app.router.lifespan_context
        previous = os.environ.get("QUANT_WRITE_API_KEY")
        os.environ["QUANT_WRITE_API_KEY"] = "test-write-key"
        try:
            app.router.lifespan_context = no_lifespan
            with TestClient(app) as client:
                self.assertEqual(client.get("/openapi.json").status_code, 200)
                self.assertEqual(client.post("/api/v1/market/bars/import", json={}).status_code, 401)
                self.assertEqual(client.post("/api/v1/market/bars/import", json={}, headers={"X-Quant-Write-Key": "wrong"}).status_code, 401)
                self.assertEqual(client.post("/api/v1/market/bars/import", json={}, headers={"X-Quant-Write-Key": "test-write-key"}).status_code, 422)
        finally:
            app.router.lifespan_context = original_lifespan
            if previous is None:
                os.environ.pop("QUANT_WRITE_API_KEY", None)
            else:
                os.environ["QUANT_WRITE_API_KEY"] = previous

    def test_executor_saturation_has_a_retryable_http_response(self) -> None:
        response = __import__("asyncio").run(executor_saturated_response(None, ExecutorSaturatedError("full")))
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"temporarily saturated", response.body)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class UpsertBarSqlIntegrationTests(unittest.TestCase):
    symbol = "999999.SZ"
    trading_date = date(2099, 1, 2)
    source = "p0-sql-regression"

    def _cleanup(self) -> None:
        # Delete in dependency order.  The reserved symbol/date/source make
        # this safe even when this test is interrupted and rerun.
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
                (self.symbol, self.trading_date),
            )
            connection.execute(
                "DELETE FROM quant.market_bars_daily WHERE symbol=%s AND trading_date=%s",
                (self.symbol, self.trading_date),
            )
            connection.execute(
                "DELETE FROM quant.raw_market_observations WHERE provider_key=%s AND symbol=%s",
                (self.source, self.symbol),
            )
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_null_control_fields_do_not_erase_existing_sql_values(self) -> None:
        self._cleanup()
        observed = datetime(2099, 1, 2, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                upsert_bar(connection, DailyBar(
                    symbol=self.symbol, trading_date=self.trading_date,
                    open=Decimal("10"), high=Decimal("10"), low=Decimal("10"), close=Decimal("10"),
                    adj_factor=Decimal("1.25"), is_suspended=True, is_st=True,
                    source=self.source, available_at=observed,
                ))
                # A normal OHLC feed has no control-plane flags or adjustment
                # factor.  SQL CASE/coalesce must preserve known values.
                upsert_bar(connection, DailyBar(
                    symbol=self.symbol, trading_date=self.trading_date,
                    open=Decimal("10"), high=Decimal("10"), low=Decimal("10"), close=Decimal("10"),
                    adj_factor=None, is_suspended=None, is_st=None,
                    source=self.source, available_at=observed,
                ))
                instrument = connection.execute(
                    "SELECT is_st FROM quant.instruments WHERE symbol=%s", (self.symbol,)
                ).fetchone()
                market = connection.execute(
                    "SELECT adj_factor,is_suspended FROM quant.market_bars_daily WHERE symbol=%s AND trading_date=%s",
                    (self.symbol, self.trading_date),
                ).fetchone()
                canonical = connection.execute(
                    "SELECT adj_factor,is_suspended FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
                    (self.symbol, self.trading_date),
                ).fetchone()
            self.assertTrue(instrument["is_st"])
            self.assertEqual(Decimal(market["adj_factor"]), Decimal("1.25"))
            self.assertTrue(market["is_suspended"])
            self.assertEqual(Decimal(canonical["adj_factor"]), Decimal("1.25"))
            self.assertTrue(canonical["is_suspended"])
        finally:
            self._cleanup()

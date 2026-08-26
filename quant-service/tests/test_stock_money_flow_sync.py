"""Coverage for per-stock end-of-day capital flow ingestion."""

from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.main import db
from app.stock_money_flow_sync import (
    MINIMUM_COVERAGE_RATIO,
    normalize_flow_rows,
    persist_flow_rows,
    sync,
)


def _parse(value):
    text = str(value or "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8])) if len(text) == 8 and text.isdigit() else None


class NormalizeFlowRowTests(unittest.TestCase):
    trade_date = date(2026, 8, 25)

    def test_vendor_supplied_net_amount_is_kept_verbatim(self):
        rows = normalize_flow_rows("moneyflow_dc", [{
            "ts_code": "000001.SZ", "trade_date": "20260825", "net_amount": 947.77,
            "net_amount_rate": 0.82, "buy_elg_amount": -5450.94, "buy_lg_amount": 6398.72,
        }], self.trade_date, _parse)
        self.assertEqual(rows[0]["net_amount"], 947.77)
        self.assertEqual(rows[0]["net_amount_rate"], 0.82)
        self.assertEqual(rows[0]["source"], "moneyflow_dc")

    def test_net_is_derived_only_when_the_vendor_did_not_supply_one(self):
        rows = normalize_flow_rows("moneyflow", [{
            "ts_code": "000001.SZ", "trade_date": "20260825",
            "buy_elg_amount": 100.0, "buy_lg_amount": 50.0,
            "sell_elg_amount": 30.0, "sell_lg_amount": 20.0,
        }], self.trade_date, _parse)
        self.assertEqual(rows[0]["net_amount"], 100.0)

    def test_no_flow_information_at_all_stays_null_rather_than_zero(self):
        rows = normalize_flow_rows("moneyflow", [
            {"ts_code": "000001.SZ", "trade_date": "20260825"},
        ], self.trade_date, _parse)
        self.assertIsNone(rows[0]["net_amount"],
                          "absent flow must not be recorded as a real zero net")

    def test_rows_from_another_date_are_dropped(self):
        rows = normalize_flow_rows("moneyflow", [
            {"ts_code": "000001.SZ", "trade_date": "20260824", "net_amount": 5.0},
        ], self.trade_date, _parse)
        self.assertEqual(rows, [])

    def test_duplicate_symbols_collapse(self):
        rows = normalize_flow_rows("moneyflow", [
            {"ts_code": "000001.SZ", "trade_date": "20260825", "net_amount": 1.0},
            {"ts_code": "000001.SZ", "trade_date": "20260825", "net_amount": 2.0},
        ], self.trade_date, _parse)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["net_amount"], 2.0)

    def test_raw_payload_is_retained_for_vendor_specific_buckets(self):
        rows = normalize_flow_rows("moneyflow_ths", [{
            "ts_code": "000001.SZ", "trade_date": "20260825", "net_amount": 1.0,
            "vendor_only_field": "kept",
        }], self.trade_date, _parse)
        self.assertEqual(rows[0]["raw"]["vendor_only_field"], "kept")


class SyncCoverageGateTests(unittest.TestCase):
    trade_date = date(2026, 8, 25)

    class _FakeConnection:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            return self

    class _FakeDb:
        def __init__(self):
            self.connection = SyncCoverageGateTests._FakeConnection()

        @contextmanager
        def transaction(self):
            yield self.connection

    def _sync(self, rows_by_api, expected=100):
        calls = []

        async def call_api(api_name, params, fields, preference):
            calls.append(api_name)
            rows = rows_by_api.get(api_name)
            if isinstance(rows, Exception):
                raise rows
            return SimpleNamespace(rows=rows, provider=SimpleNamespace(key="tushare_test"))

        async def run_blocking(fn, *args, **kwargs):
            return fn(*args) if args else fn()

        return asyncio.run(sync(
            self.trade_date, call_tushare_api=call_api, parse_date=_parse,
            expected_symbols=lambda _d: expected, run_database_blocking=run_blocking,
            db=self._FakeDb(), safe_error_detail=lambda text, _n: text,
        )), calls

    @staticmethod
    def _rows(count):
        return [{"ts_code": f"{index:06d}.SZ", "trade_date": "20260825", "net_amount": 1.0}
                for index in range(count)]

    def test_a_truncated_cross_section_is_rejected_not_stored(self):
        result, _ = self._sync({"moneyflow": self._rows(50), "moneyflow_dc": self._rows(90),
                                "moneyflow_ths": self._rows(95)}, expected=100)
        self.assertEqual(result["status"], "partial")
        self.assertIn("moneyflow", result["errors"])
        self.assertIn("coverage floor", result["errors"]["moneyflow"])
        self.assertNotIn("moneyflow", result["rows"])
        self.assertIn("moneyflow_dc", result["rows"])

    def test_one_vendor_outage_does_not_block_the_others(self):
        result, _ = self._sync({"moneyflow": RuntimeError("gateway down"),
                                "moneyflow_dc": self._rows(95), "moneyflow_ths": self._rows(95)})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"]["moneyflow"], "gateway down")
        self.assertEqual(set(result["rows"]), {"moneyflow_dc", "moneyflow_ths"})

    def test_all_sources_failing_blocks_rather_than_storing_nothing_silently(self):
        result, _ = self._sync({name: self._rows(1) for name in
                                ("moneyflow", "moneyflow_dc", "moneyflow_ths")}, expected=100)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("coverage gate", result["reason"])

    def test_no_daily_universe_blocks_before_any_provider_call(self):
        result, calls = self._sync({}, expected=0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, [], "the universe check must precede provider I/O")

    def test_completed_run_states_the_end_of_day_boundary(self):
        result, _ = self._sync({name: self._rows(95) for name in
                                ("moneyflow", "moneyflow_dc", "moneyflow_ths")})
        self.assertEqual(result["status"], "completed")
        self.assertIn("end_of_day_only", result["boundary"])
        self.assertEqual(MINIMUM_COVERAGE_RATIO, 0.80)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class StockMoneyFlowPersistenceTests(unittest.TestCase):
    symbol = "999978.SZ"
    trade_date = date(2099, 4, 1)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.stock_money_flow_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT DO NOTHING",
                (self.symbol,),
            )

    def test_three_vendors_coexist_and_reingest_updates_in_place(self) -> None:
        stamp = datetime(2099, 4, 1, tzinfo=timezone.utc)
        rows = [{"symbol": self.symbol, "trading_date": self.trade_date, "source": source,
                 "net_amount": value, "net_amount_rate": None, "buy_elg_amount": None,
                 "buy_lg_amount": None, "buy_md_amount": None, "buy_sm_amount": None, "raw": {}}
                for source, value in (("moneyflow", 1.0), ("moneyflow_dc", 2.0), ("moneyflow_ths", 3.0))]
        with db.transaction() as connection:
            persist_flow_rows(connection, rows, "tushare_test", stamp)
            stored = connection.execute(
                "SELECT source,net_amount FROM quant.stock_money_flow_daily WHERE symbol=%s ORDER BY source",
                (self.symbol,),
            ).fetchall()
        self.assertEqual([(r["source"], float(r["net_amount"])) for r in stored],
                         [("moneyflow", 1.0), ("moneyflow_dc", 2.0), ("moneyflow_ths", 3.0)])
        rows[0]["net_amount"] = 9.0
        with db.transaction() as connection:
            persist_flow_rows(connection, rows, "tushare_test", stamp)
            again = connection.execute(
                "SELECT count(*) n, max(net_amount) FILTER (WHERE source='moneyflow') v "
                "FROM quant.stock_money_flow_daily WHERE symbol=%s", (self.symbol,),
            ).fetchone()
        self.assertEqual(again["n"], 3)
        self.assertEqual(float(again["v"]), 9.0)

    def test_an_unknown_symbol_is_skipped_rather_than_violating_the_instrument_key(self) -> None:
        with db.transaction() as connection:
            persist_flow_rows(connection, [{
                "symbol": "999666.SZ", "trading_date": self.trade_date, "source": "moneyflow",
                "net_amount": 1.0, "net_amount_rate": None, "buy_elg_amount": None,
                "buy_lg_amount": None, "buy_md_amount": None, "buy_sm_amount": None, "raw": {},
            }], "tushare_test", datetime(2099, 4, 1, tzinfo=timezone.utc))
            count = connection.execute(
                "SELECT count(*) n FROM quant.stock_money_flow_daily WHERE symbol='999666.SZ'").fetchone()
        self.assertEqual(count["n"], 0)


if __name__ == "__main__":
    unittest.main()

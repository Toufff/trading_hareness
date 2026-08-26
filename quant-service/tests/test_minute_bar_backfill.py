"""Coverage for the candidate-scoped historical minute-bar backfill."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.main import db
from app.minute_bar_backfill import (
    CN_TZ,
    ensure_import_record,
    FULL_SESSION_BARS,
    backfill_symbol_session,
    in_session,
    limit_up_symbols,
    normalize_minute_rows,
    parse_trade_time,
    persist_minute_rows,
    reconcile_against_daily,
)


def _bar(symbol: str, stamp: str, close: float = 10.0, **extra) -> dict:
    return {"ts_code": symbol, "trade_time": stamp, "freq": "1min",
            "open": close, "high": close, "low": close, "close": close,
            "vol": 1000, "amount": close * 1000, **extra}


class TradeTimeParsingTests(unittest.TestCase):
    def test_offset_free_stamps_are_read_as_exchange_local(self):
        parsed = parse_trade_time("2026-08-25 09:30:00")
        self.assertEqual(parsed.tzinfo, CN_TZ)
        self.assertEqual(parsed.hour, 9)

    def test_fractional_seconds_are_accepted(self):
        self.assertIsNotNone(parse_trade_time("2026-08-25 15:34:04.000"))

    def test_unparseable_values_are_rejected(self):
        for value in (None, "", "not-a-time", 12345):
            with self.subTest(value=value):
                self.assertIsNone(parse_trade_time(value))


class SessionWindowTests(unittest.TestCase):
    def test_continuous_auction_is_in_session(self):
        for stamp in ("2026-08-25 09:30:00", "2026-08-25 11:30:00",
                      "2026-08-25 13:00:00", "2026-08-25 15:00:00"):
            with self.subTest(stamp=stamp):
                self.assertTrue(in_session(parse_trade_time(stamp)))

    def test_auction_and_after_hours_prints_are_out_of_session(self):
        # The upstream returns these on recent dates; a 09:25 auction print or
        # a 15:34 after-hours row is not a minute bar.
        for stamp in ("2026-08-25 09:25:02", "2026-08-25 15:34:04", "2026-08-25 12:00:00"):
            with self.subTest(stamp=stamp):
                self.assertFalse(in_session(parse_trade_time(stamp)))


class NormalizeMinuteRowTests(unittest.TestCase):
    symbol = "000001.SZ"

    def test_out_of_session_rows_are_dropped(self):
        rows = normalize_minute_rows(self.symbol, [
            _bar(self.symbol, "2026-08-25 09:25:02"),
            _bar(self.symbol, "2026-08-25 09:30:00"),
            _bar(self.symbol, "2026-08-25 15:34:04"),
        ])
        self.assertEqual([row["bar_time"].strftime("%H:%M") for row in rows], ["09:30"])

    def test_rows_for_another_symbol_are_dropped(self):
        rows = normalize_minute_rows(self.symbol, [_bar("600519.SH", "2026-08-25 09:30:00")])
        self.assertEqual(rows, [])

    def test_output_is_oldest_first_and_deduplicated(self):
        rows = normalize_minute_rows(self.symbol, [
            _bar(self.symbol, "2026-08-25 09:32:00", close=3.0),
            _bar(self.symbol, "2026-08-25 09:30:00", close=1.0),
            _bar(self.symbol, "2026-08-25 09:30:00", close=2.0),
        ])
        self.assertEqual([row["close"] for row in rows], [2.0, 3.0])

    def test_a_jittered_redelivery_collapses_into_its_minute(self):
        # The upstream sends most minutes twice: once on the minute and once
        # a few seconds later.  Counting both would double every volume sum.
        rows = normalize_minute_rows(self.symbol, [
            _bar(self.symbol, "2026-08-25 09:32:00", close=8.6),
            _bar(self.symbol, "2026-08-25 09:32:07", close=8.6),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bar_time"].strftime("%H:%M:%S"), "09:32:00")

    def test_the_exact_minute_stamp_wins_when_the_copies_disagree(self):
        # Observed on 000017.SZ 14:15 for 2026-08-25: the on-the-minute row
        # carried the full 12718 shares, the jittered copy only 4100.
        for order in ([0, 1], [1, 0]):
            candidates = [_bar(self.symbol, "2026-08-25 14:15:00", close=8.6, vol=12718),
                          _bar(self.symbol, "2026-08-25 14:15:58", close=8.6, vol=4100)]
            rows = normalize_minute_rows(self.symbol, [candidates[i] for i in order])
            with self.subTest(order=order):
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["volume"], 12718)

    def test_a_jittered_row_is_kept_when_no_exact_stamp_exists(self):
        rows = normalize_minute_rows(self.symbol, [_bar(self.symbol, "2026-08-25 14:59:37", close=8.6)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bar_time"].strftime("%H:%M:%S"), "14:59:00")

    def test_a_nonpositive_close_is_not_a_bar(self):
        self.assertEqual(normalize_minute_rows(self.symbol, [
            _bar(self.symbol, "2026-08-25 09:30:00", close=0.0),
        ]), [])


class BackfillOrchestrationTests(unittest.TestCase):
    def _run(self, rows):
        captured = {}

        async def call_api(api_name, params, fields, preference):
            captured["api"] = api_name
            captured["params"] = params
            captured["preference"] = preference
            return SimpleNamespace(rows=rows, provider=SimpleNamespace(key="tushare_super_get"))

        async def run_blocking(fn, **kwargs):
            return fn()

        result = asyncio.run(backfill_symbol_session(
            "000001.SZ", date(2026, 8, 25), call_tushare_api=call_api,
            run_database_blocking=run_blocking, db=SimpleNamespace(transaction=_FakeTx),
        ))
        return result, captured

    def test_it_requests_one_bounded_symbol_from_the_get_gateway(self):
        _, captured = self._run([_bar("000001.SZ", "2026-08-25 09:30:00")])
        self.assertEqual(captured["api"], "stk_mins")
        self.assertEqual(captured["preference"], "super_get")
        self.assertEqual(captured["params"]["ts_code"], "000001.SZ")
        self.assertEqual(captured["params"]["freq"], "1min")
        self.assertNotIn(",", captured["params"]["ts_code"], "the route serves one symbol per call")

    def test_a_short_session_is_reported_partial_rather_than_complete(self):
        result, _ = self._run([_bar("000001.SZ", "2026-08-25 09:30:00")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["bars"], 1)
        self.assertEqual(result["expected"], FULL_SESSION_BARS)
        self.assertEqual(result["reconciliation"]["status"], "unverifiable")

    def test_an_empty_response_is_reported_not_raised(self):
        result, _ = self._run([])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["bars"], 0)


class _FakeConnection:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        # The orchestration tests exercise fetch/persist, not reconciliation;
        # an absent daily bar makes the check report "unverifiable".
        return None


class _FakeTx:
    def __enter__(self):
        return _FakeConnection()

    def __exit__(self, *_exc):
        return False


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class MinuteBackfillIntegrationTests(unittest.TestCase):
    symbol = "999975.SZ"
    trading_date = date(2099, 5, 4)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.market_bars_minute WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.offline_imports WHERE source_name='tushare_stk_mins'"
                               " AND file_name LIKE %s", (f"%{self.symbol}%",))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT DO NOTHING",
                (self.symbol,),
            )

    def test_bars_persist_and_reingest_updates_in_place(self) -> None:
        stamp = datetime(2099, 5, 4, tzinfo=timezone.utc)
        rows = normalize_minute_rows(self.symbol, [_bar(self.symbol, "2099-05-04 09:30:00", close=10.0)])
        with db.transaction() as connection:
            import_id = ensure_import_record(connection, self.symbol, self.trading_date, len(rows), stamp)
            persist_minute_rows(connection, rows, stamp, import_id)
        rows[0]["close"] = 11.0
        with db.transaction() as connection:
            # A re-run must reuse the same deterministic import record.
            again = ensure_import_record(connection, self.symbol, self.trading_date, len(rows), stamp)
            self.assertEqual(again, import_id)
            persist_minute_rows(connection, rows, stamp, again)
            stored = connection.execute(
                "SELECT count(*) n, max(close) c FROM quant.market_bars_minute WHERE symbol=%s",
                (self.symbol,),
            ).fetchone()
        self.assertEqual(stored["n"], 1)
        self.assertEqual(float(stored["c"]), 11.0)

    def test_reconciliation_is_exact_for_a_complete_session(self) -> None:
        stamp = datetime(2099, 5, 4, tzinfo=timezone.utc)
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.canonical_bars_daily(
                       symbol,trading_date,close,volume,is_suspended,available_at,selected_provider)
                   VALUES(%s,%s,10.0,%s,false,%s,'test')""",
                (self.symbol, self.trading_date, 30.0, stamp),  # 30 手 = 3000 shares
            )
            rows = normalize_minute_rows(self.symbol, [
                _bar(self.symbol, "2099-05-04 09:30:00", close=10.0, vol=1000),
                _bar(self.symbol, "2099-05-04 09:31:00", close=10.0, vol=2000),
            ])
            import_id = ensure_import_record(connection, self.symbol, self.trading_date, len(rows), stamp)
            persist_minute_rows(connection, rows, stamp, import_id)
            result = reconcile_against_daily(connection, self.symbol, self.trading_date)
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["difference_pct"], 0.0)
        self.assertEqual(result["bars"], 2)

    def test_a_truncated_session_is_reported_as_a_shortfall(self) -> None:
        stamp = datetime(2099, 5, 4, tzinfo=timezone.utc)
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.canonical_bars_daily(
                       symbol,trading_date,close,volume,is_suspended,available_at,selected_provider)
                   VALUES(%s,%s,10.0,%s,false,%s,'test')""",
                (self.symbol, self.trading_date, 100.0, stamp),  # 10000 shares expected
            )
            rows = normalize_minute_rows(self.symbol, [
                _bar(self.symbol, "2099-05-04 09:30:00", close=10.0, vol=1000),
            ])
            import_id = ensure_import_record(connection, self.symbol, self.trading_date, len(rows), stamp)
            persist_minute_rows(connection, rows, stamp, import_id)
            result = reconcile_against_daily(connection, self.symbol, self.trading_date)
        self.assertEqual(result["status"], "shortfall")
        self.assertEqual(result["difference_pct"], -90.0)

    def test_reconciliation_without_a_daily_bar_is_unverifiable(self) -> None:
        with db.transaction() as connection:
            result = reconcile_against_daily(connection, self.symbol, self.trading_date)
        self.assertEqual(result["status"], "unverifiable")

    def test_limit_up_symbols_selects_only_locked_closes(self) -> None:
        stamp = datetime(2099, 5, 4, tzinfo=timezone.utc)
        with db.transaction() as connection:
            for offset, (close, limit_up) in enumerate(((11.0, 11.0), (10.0, 11.0))):
                connection.execute(
                    """INSERT INTO quant.canonical_bars_daily(
                           symbol,trading_date,close,limit_up,volume,is_suspended,available_at,selected_provider)
                       VALUES(%s,%s,%s,%s,100,false,%s,'test')""",
                    (self.symbol, date(2099, 5, 4 + offset), close, limit_up, stamp),
                )
            sessions = limit_up_symbols(connection, date(2099, 5, 4), date(2099, 5, 5))
        self.assertEqual(sessions.get(date(2099, 5, 4)), [self.symbol])
        self.assertNotIn(date(2099, 5, 5), sessions, "a close below the limit is not a locked close")


if __name__ == "__main__":
    unittest.main()

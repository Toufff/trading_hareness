from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.public_market_repository import (
    persist_free_daily,
    persist_free_quotes,
    persist_market_events,
    persist_public_observations,
)


@dataclass
class _CapturedBar:
    symbol: str
    trading_date: date
    close: Any = None
    open: Any = None
    high: Any = None
    low: Any = None
    volume: Any = None
    amount: Any = None
    source: str = "akshare"
    available_at: Any = None


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _parse_trade_date(value: Any) -> date | None:
    if not value:
        return None
    return datetime.strptime(str(value), "%Y%m%d").date()


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> "_RecordingConnection":
        self.executed.append((sql, params))
        return self

    def fetchone(self) -> None:
        return None


class _RecordingTransaction:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _RecordingConnection:
        return self._connection

    def __exit__(self, *args: Any) -> None:
        return None


class _RecordingDatabase:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction(self.connection)


def _upsert_bar(_connection: Any, bar: Any) -> None:
    return None


class PersistFreeDailyUnsettledGuardTests(unittest.TestCase):
    def test_same_day_row_before_settlement_is_rejected_and_recorded(self) -> None:
        # 2026-09-01 10:00 Shanghai (02:00 UTC) is well before the 15:05
        # settlement floor, so today's own row must not be promoted.
        observed_at = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
        db = _RecordingDatabase()
        stored = persist_free_daily(
            db, "akshare",
            [{"ts_code": "600000.SH", "trade_date": "20260901", "close": "10.5"}],
            daily_bar_type=_CapturedBar, parse_trade_date=_parse_trade_date,
            decimal_or_none=_decimal_or_none, upsert_bar=_upsert_bar,
            persist_raw_observations=lambda *_a, **_k: 0,
            observed_at=observed_at,
        )
        self.assertEqual(stored, 0)
        issue_sql = [sql for sql, _params in db.connection.executed if "data_quality_issues" in sql]
        self.assertEqual(len(issue_sql), 1)
        self.assertIn("unsettled_session_daily_row", issue_sql[0])

    def test_same_day_row_after_settlement_is_accepted(self) -> None:
        # 2026-09-01 15:10 Shanghai (07:10 UTC) is past the 15:05 floor.
        observed_at = datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc)
        db = _RecordingDatabase()
        stored = persist_free_daily(
            db, "akshare",
            [{"ts_code": "600000.SH", "trade_date": "20260901", "close": "10.5"}],
            daily_bar_type=_CapturedBar, parse_trade_date=_parse_trade_date,
            decimal_or_none=_decimal_or_none, upsert_bar=_upsert_bar,
            persist_raw_observations=lambda *_a, **_k: 0,
            observed_at=observed_at,
        )
        self.assertEqual(stored, 1)

    def test_historical_row_is_accepted_regardless_of_time_of_day(self) -> None:
        observed_at = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
        db = _RecordingDatabase()
        stored = persist_free_daily(
            db, "akshare",
            [{"ts_code": "600000.SH", "trade_date": "20260810", "close": "10.5"}],
            daily_bar_type=_CapturedBar, parse_trade_date=_parse_trade_date,
            decimal_or_none=_decimal_or_none, upsert_bar=_upsert_bar,
            persist_raw_observations=lambda *_a, **_k: 0,
            observed_at=observed_at,
        )
        self.assertEqual(stored, 1)

    def test_malformed_row_is_counted_and_recorded_not_silently_dropped(self) -> None:
        observed_at = datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc)
        db = _RecordingDatabase()
        stored = persist_free_daily(
            db, "akshare",
            [{"ts_code": "not-a-symbol", "trade_date": "20260810", "close": "10.5"}],
            daily_bar_type=_CapturedBar, parse_trade_date=_parse_trade_date,
            decimal_or_none=_decimal_or_none, upsert_bar=_upsert_bar,
            persist_raw_observations=lambda *_a, **_k: 0,
            observed_at=observed_at,
        )
        self.assertEqual(stored, 0)
        issue_sql = [sql for sql, _params in db.connection.executed if "malformed_free_daily_rows" in sql]
        self.assertEqual(len(issue_sql), 1)


class PersistMarketEventsNaiveTimestampTests(unittest.TestCase):
    def test_naive_published_at_is_rejected_and_recorded(self) -> None:
        db = _RecordingDatabase()
        stored = persist_market_events(db, "akshare", [{
            "ts_code": "600000.SH", "title": "涨停：示例",
            "published_at": "2026-09-01T00:00:00",  # naive
            "event_type": "limit_up_pool",
        }])
        self.assertEqual(stored, 0)
        issue_sql = [sql for sql, _params in db.connection.executed if "naive_published_at_timestamp" in sql]
        self.assertEqual(len(issue_sql), 1)
        # No market_events insert should have been attempted for the rejected row.
        self.assertFalse(any("market_events" in sql for sql, _params in db.connection.executed))

    def test_aware_published_at_is_accepted(self) -> None:
        db = _RecordingDatabase()
        stored = persist_market_events(db, "akshare", [{
            "ts_code": "600000.SH", "title": "涨停：示例",
            "published_at": "2026-09-01T15:30:00+08:00",
            "event_type": "limit_up_pool", "availability_basis": "post_close_publication",
        }])
        self.assertEqual(stored, 1)
        insert_calls = [(sql, params) for sql, params in db.connection.executed if "INSERT INTO quant.market_events" in sql]
        self.assertEqual(len(insert_calls), 1)
        self.assertIn("post_close_publication", insert_calls[0][1])

    def test_least_merge_is_gated_by_matching_availability_basis(self) -> None:
        db = _RecordingDatabase()
        persist_market_events(db, "akshare", [{
            "ts_code": "600000.SH", "title": "涨停：示例",
            "published_at": "2026-09-01T15:30:00+08:00",
            "event_type": "limit_up_pool", "availability_basis": "post_close_publication",
        }])
        insert_sql = [sql for sql, _params in db.connection.executed if "INSERT INTO quant.market_events" in sql][0]
        self.assertIn("coalesce(quant.market_events.availability_basis,'unknown')=coalesce(EXCLUDED.availability_basis,'unknown')", insert_sql)


class PersistFreeQuotesBatchingTests(unittest.TestCase):
    """persist_free_quotes previously ran one INSERT per quote; it is now one
    set-based upsert regardless of batch size (see the WP10 N+1 fix)."""

    def test_valid_quotes_are_written_in_exactly_one_statement(self) -> None:
        db = _RecordingDatabase()
        stored = persist_free_quotes(db, "tencent_free", [
            {"ts_code": "000001.SZ", "price": 10.0},
            {"ts_code": "600000.SH", "price": 12.0},
        ])
        self.assertEqual(stored, 2)
        insert_calls = [(sql, params) for sql, params in db.connection.executed
                        if "INSERT INTO quant.raw_market_observations" in sql]
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(set(params["symbols"]), {"000001.SZ", "600000.SH"})

    def test_malformed_symbols_are_skipped_without_a_statement(self) -> None:
        db = _RecordingDatabase()
        stored = persist_free_quotes(db, "tencent_free", [{"ts_code": "not-a-symbol", "price": 1.0}])
        self.assertEqual(stored, 0)
        self.assertEqual(db.connection.executed, [])

    def test_duplicate_symbol_and_payload_pairs_are_deduplicated(self) -> None:
        db = _RecordingDatabase()
        quote = {"ts_code": "000001.SZ", "price": 10.0}
        stored = persist_free_quotes(db, "tencent_free", [dict(quote), dict(quote)])
        self.assertEqual(stored, 1)
        _sql, params = db.connection.executed[0]
        self.assertEqual(len(params["symbols"]), 1)


class PersistPublicObservationsBatchingTests(unittest.TestCase):
    def test_rows_are_written_in_exactly_one_statement(self) -> None:
        db = _RecordingDatabase()
        stored = persist_public_observations(db, "akshare", "daily_bar", [
            {"ts_code": "000001.SZ", "value": 1},
            {"ts_code": "600000.SH", "value": 2},
        ])
        self.assertEqual(stored, 2)
        insert_calls = [(sql, params) for sql, params in db.connection.executed
                        if "INSERT INTO quant.raw_market_observations" in sql]
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(set(params["symbols"]), {"000001.SZ", "600000.SH"})
        self.assertEqual(params["capability"], "daily_bar")

    def test_rows_without_a_valid_symbol_still_store_with_null_symbol(self) -> None:
        db = _RecordingDatabase()
        stored = persist_public_observations(db, "akshare", "market_summary", [{"value": 1}, {"value": 2}])
        self.assertEqual(stored, 2)
        _sql, params = [(sql, params) for sql, params in db.connection.executed
                        if "INSERT INTO quant.raw_market_observations" in sql][0]
        self.assertEqual(params["symbols"], [None, None])

    def test_record_index_keeps_otherwise_identical_rows_distinct(self) -> None:
        db = _RecordingDatabase()
        stored = persist_public_observations(db, "akshare", "market_summary", [
            {"ts_code": "885001.TI", "value": 1}, {"ts_code": "885001.TI", "value": 1},
        ])
        self.assertEqual(stored, 2)
        _sql, params = [(sql, params) for sql, params in db.connection.executed
                        if "INSERT INTO quant.raw_market_observations" in sql][0]
        self.assertEqual(len(set(params["shas"])), 2)


if __name__ == "__main__":
    unittest.main()

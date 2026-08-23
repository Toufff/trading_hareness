from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import unittest

from app.strategy_context_read_model import (
    event_context,
    index_breadth_context,
    source_readiness,
    tushare_lhb_context,
)


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement, params=()):
        self.calls.append((statement, params))
        if "market_snapshot_runs" in statement:
            return _Result(row={
                "observed_at": datetime(2026, 8, 21, 7, 30, tzinfo=timezone.utc),
                "status": "completed", "coverage": 1, "summary": {"priced_symbols": 2, "advancers": 3, "decliners": 1},
                "quality_flags": ["saved_snapshot"], "source_summary": {},
            })
        if "canonical_bars_daily" in statement:
            return _Result(row={
                "trading_date": date(2026, 8, 21), "close": 10, "pre_close": 9, "available_at": datetime(2026, 8, 21, 7, tzinfo=timezone.utc),
            })
        if "market_bars_daily" in statement:
            return _Result(rows=[{"symbol": "000300.SH", "trading_date": "2026-08-21", "close": 10}])
        if "provider_health" in statement:
            return _Result(rows=[{"provider_key": "tencent_free", "capability": "quote", "last_success_at": None,
                                  "last_failure_at": None, "last_row_count": 1, "consecutive_failures": 0}])
        if "GROUP BY source,event_type" in statement:
            return _Result(rows=[{"source": "akshare", "event_type": "limit_up_pool", "latest_available_at": None, "rows": 2}])
        if "tushare_raw_records" in statement:
            return _Result(rows=[{"api_name": "top_inst", "available_at": datetime(2026, 8, 21, 8, tzinfo=timezone.utc),
                                  "row_data": {"ts_code": "000001.SZ", "net_buy": 3}}])
        if "market_events" in statement:
            return _Result(rows=[{"symbol": "000001.SZ", "event_type": "limit_up_pool", "title": "A", "available_at": datetime(2026, 8, 21, 8, tzinfo=timezone.utc)}])
        raise AssertionError(statement)


class _Database:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        yield self.connection


class StrategyContextReadModelTests(unittest.TestCase):
    def setUp(self):
        self.connection = _Connection()
        self.database = _Database(self.connection)
        self.observed_at = datetime(2026, 8, 21, 8, 30, tzinfo=timezone.utc)

    def test_index_context_is_checkpoint_bounded(self):
        result = index_breadth_context(
            self.connection, date(2026, 8, 21), "close", self.observed_at,
            index_symbols=("000300.SH",),
            index_regime=lambda rows: {"items": [{"symbol": "000300.SH", "trading_date": "2026-08-21", "rows": len(rows)}]},
            number=float,
        )
        self.assertEqual(result["breadth"]["state"], "broad_positive")
        self.assertEqual(result["index"]["change_pct"], 11.1111)
        self.assertIn("saved_snapshot", result["quality_flags"])
        self.assertTrue(all(self.observed_at in params for _sql, params in self.connection.calls[:3]))

    def test_event_and_lhb_contexts_group_saved_rows_by_symbol(self):
        events = event_context(self.database, ["000001.SZ"], self.observed_at)
        lhb = tushare_lhb_context(self.database, ["000001.SZ"], self.observed_at)
        self.assertEqual(events["000001.SZ"][0]["event_type"], "limit_up_pool")
        self.assertEqual(lhb["000001.SZ"][0]["api_name"], "top_inst")

    def test_source_readiness_exposes_unconfigured_xinhua_without_guessing_endpoint(self):
        result = source_readiness(
            self.database, self.observed_at,
            provider_status=lambda: [{"provider_key": "xinhua_finance", "configured": False}],
            json_safe=lambda value: value,
        )
        self.assertEqual(result["xinhua_finance"]["status"], "not_configured")
        self.assertIn("tencent_free", result["providers"])
        self.assertEqual(result["post_close_event_inventory"][0]["event_type"], "limit_up_pool")


if __name__ == "__main__":
    unittest.main()

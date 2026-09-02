"""Coverage for the daily/index_daily batching path in tushare_normalization.

Previously ``normalize_rows`` called the injected ``upsert_bar`` once per row
for the ``daily``/``index_daily`` APIs -- the core of the post-close N+1 (5-6
statements x ~5,500 symbols).  It now collects parsed ``DailyBar`` objects and
writes them through ``daily_bar_batch_repository.upsert_daily_bars`` in one
call, falling back to the original per-row path only if the batch call itself
raises.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import unittest

from app.request_models import DailyBar
from app.tushare_normalization import normalize_rows


def _decimal_or_none(value):
    return Decimal(str(value)) if value not in (None, "") else None


class _RecordingConnection:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None


def _call(connection, rows, api_name="daily", upsert_bar=None):
    return normalize_rows(
        connection, api_name, rows, datetime(2026, 8, 20, tzinfo=timezone.utc),
        core_apis=frozenset({"daily", "index_daily", "adj_factor"}),
        date_parser=lambda value: date(2026, 8, 20) if value else None,
        exchange_for=lambda symbol: symbol.rsplit(".", 1)[1],
        is_st_security_name=lambda _name: False,
        ensure_instrument=lambda *_args: None,
        upsert_bar=upsert_bar or (lambda *_args: None),
        daily_bar_type=DailyBar,
        decimal_or_none=_decimal_or_none,
        safe_error_detail=lambda message, limit: message[:limit],
    )


class TushareNormalizationDailyBatchingTests(unittest.TestCase):
    def test_daily_rows_are_written_through_one_batched_call_not_per_row_upsert(self) -> None:
        connection = _RecordingConnection()
        upsert_bar = unittest.mock.Mock()
        rows = [
            {"ts_code": "000001.SZ", "trade_date": "20260820", "close": "10.5", "vol": "1000", "amount": "500"},
            {"ts_code": "600000.SH", "trade_date": "20260820", "close": "12.0", "vol": "2000", "amount": "800"},
        ]
        with patch("app.tushare_normalization.upsert_daily_bars") as batched:
            normalized = _call(connection, rows, upsert_bar=upsert_bar)
        self.assertEqual(normalized, 2)
        batched.assert_called_once()
        batch_connection, batch_bars = batched.call_args.args
        self.assertIs(batch_connection, connection)
        self.assertEqual({bar.symbol for bar in batch_bars}, {"000001.SZ", "600000.SH"})
        upsert_bar.assert_not_called()

    def test_batch_failure_falls_back_to_the_per_row_path(self) -> None:
        connection = _RecordingConnection()
        upsert_bar = unittest.mock.Mock()
        rows = [
            {"ts_code": "000001.SZ", "trade_date": "20260820", "close": "10.5", "vol": "1000", "amount": "500"},
            {"ts_code": "600000.SH", "trade_date": "20260820", "close": "12.0", "vol": "2000", "amount": "800"},
        ]
        with patch("app.tushare_normalization.upsert_daily_bars", side_effect=RuntimeError("constraint violated")):
            normalized = _call(connection, rows, upsert_bar=upsert_bar)
        self.assertEqual(normalized, 2)
        self.assertEqual(upsert_bar.call_count, 2)
        self.assertEqual({call.args[1].symbol for call in upsert_bar.call_args_list}, {"000001.SZ", "600000.SH"})

    def test_batch_failure_and_a_genuinely_bad_bar_records_one_quality_issue(self) -> None:
        connection = _RecordingConnection()

        def failing_upsert_bar(_connection, bar):
            if bar.symbol == "600000.SH":
                raise ValueError("simulated per-row failure")

        rows = [
            {"ts_code": "000001.SZ", "trade_date": "20260820", "close": "10.5", "vol": "1000", "amount": "500"},
            {"ts_code": "600000.SH", "trade_date": "20260820", "close": "12.0", "vol": "2000", "amount": "800"},
        ]
        with patch("app.tushare_normalization.upsert_daily_bars", side_effect=RuntimeError("batch failed")):
            normalized = _call(connection, rows, upsert_bar=failing_upsert_bar)
        self.assertEqual(normalized, 1)
        issue_calls = [params for sql, params in connection.calls if "data_quality_issues" in sql]
        self.assertEqual(len(issue_calls), 1)
        self.assertEqual(issue_calls[0][2].obj["symbol"], "600000.SH")

    def test_non_daily_apis_are_unaffected_by_the_batching_path(self) -> None:
        connection = _RecordingConnection()
        with patch("app.tushare_normalization.upsert_daily_bars") as batched:
            normalized = _call(
                connection, [{"ts_code": "000001.SZ", "trade_date": "20260820", "adj_factor": "1.25"}],
                api_name="adj_factor",
            )
        self.assertEqual(normalized, 1)
        batched.assert_not_called()
        self.assertTrue(any("daily_adjustment_factors" in sql for sql, _params in connection.calls))


if __name__ == "__main__":
    unittest.main()

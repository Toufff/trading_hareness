"""Coverage for the quote-row batching in longhu_market_repository.

``persist_full_market_close`` previously ran one INSERT per Tencent quote row
(one row per A-share symbol, ~5,500 for a full close).  It now writes the
whole batch through one ``unnest``-driven upsert.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.longhu_market_repository import persist_full_market_close
from app.longhu_market_sync import MergedCrossSection


class _RecordingConnection:
    def __init__(self, fetch_run_id=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._fetch_run_id = fetch_run_id
        self._last_sql = ""

    rowcount = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self._last_sql = normalized
        return self

    def fetchone(self):
        if "market_trade_calendar" in self._last_sql:
            return {"prior_date": None}
        if "fetch_run_id FROM quant.fetch_runs" in self._last_sql:
            return {"fetch_run_id": self._fetch_run_id} if self._fetch_run_id else None
        if "intraday_board_reports" in self._last_sql:
            return None
        return None


def _merged(quote_rows: list[dict]) -> MergedCrossSection:
    return MergedCrossSection(
        daily_rows=[], fundamental_rows=[], flow_rows=[], quote_rows=quote_rows,
        coverage=1.0, close_conflicts=(),
    )


class PersistFullMarketCloseQuoteBatchingTests(unittest.TestCase):
    def test_quote_rows_are_written_in_exactly_one_statement(self) -> None:
        connection = _RecordingConnection(fetch_run_id="run-1")
        quote_rows = [
            {"ts_code": "000001.SZ", "price": 10.0},
            {"ts_code": "600000.SH", "price": 12.0},
        ]
        result = persist_full_market_close(
            connection,
            trade_date=date(2026, 8, 20),
            request_key="req-1",
            observed_at=datetime(2026, 8, 20, 7, tzinfo=timezone.utc),
            merged=_merged(quote_rows),
            source_health={},
            board_rows=[],
            persist_rows=lambda *_a, **_k: 0,
            persist_flow_rows=lambda *_a, **_k: 0,
        )
        self.assertEqual(result["quote_rows"], 2)
        insert_calls = [(sql, params) for sql, params in connection.calls
                        if "INSERT INTO quant.raw_market_observations" in sql]
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(set(params["symbols"]), {"000001.SZ", "600000.SH"})
        self.assertEqual(params["fetch_run_id"], "run-1")

    def test_no_quote_rows_issues_no_statement(self) -> None:
        connection = _RecordingConnection()
        result = persist_full_market_close(
            connection,
            trade_date=date(2026, 8, 20),
            request_key="req-1",
            observed_at=datetime(2026, 8, 20, 7, tzinfo=timezone.utc),
            merged=_merged([]),
            source_health={},
            board_rows=[],
            persist_rows=lambda *_a, **_k: 0,
            persist_flow_rows=lambda *_a, **_k: 0,
        )
        self.assertEqual(result["quote_rows"], 0)
        self.assertFalse(any("raw_market_observations" in sql for sql, _params in connection.calls))

    def test_duplicate_symbol_and_payload_pairs_are_deduplicated(self) -> None:
        connection = _RecordingConnection()
        quote = {"ts_code": "000001.SZ", "price": 10.0}
        result = persist_full_market_close(
            connection,
            trade_date=date(2026, 8, 20),
            request_key="req-1",
            observed_at=datetime(2026, 8, 20, 7, tzinfo=timezone.utc),
            merged=_merged([dict(quote), dict(quote)]),
            source_health={},
            board_rows=[],
            persist_rows=lambda *_a, **_k: 0,
            persist_flow_rows=lambda *_a, **_k: 0,
        )
        self.assertEqual(result["quote_rows"], 2)
        _sql, params = [(sql, params) for sql, params in connection.calls
                        if "INSERT INTO quant.raw_market_observations" in sql][0]
        self.assertEqual(len(params["symbols"]), 1)


if __name__ == "__main__":
    unittest.main()

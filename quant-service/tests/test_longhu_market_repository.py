"""Coverage for the quote-row batching in longhu_market_repository.

``persist_full_market_close`` previously ran one INSERT per settled quote row
(one row per A-share symbol, ~5,500 for a full close).  It now writes the
whole batch through one ``unnest``-driven upsert.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.longhu_market_repository import persist_full_market_close, persist_longhu_industry_memberships
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
    def test_longhu_industry_snapshot_is_materialised_with_point_in_time_basis(self) -> None:
        connection = _RecordingConnection()
        count = persist_longhu_industry_memberships(
            connection, date(2026, 8, 20), datetime(2026, 8, 20, 7, tzinfo=timezone.utc),
            [{"symbol": "600664.SH", "raw": {
                "plate_id": "881140", "screen_snapshot": {"sector_label": "化学制药"},
            }}],
        )
        self.assertEqual(count, 1)
        membership = [(sql, params) for sql, params in connection.calls
                      if "INSERT INTO quant.sector_membership_history" in sql]
        self.assertEqual(len(membership), 1)
        sql, params = membership[0]
        self.assertIn("'observed_snapshot'", sql)
        self.assertEqual(params["symbols"], ["600664.SH"])
        self.assertEqual(params["sector_keys"], ["881140"])

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
        self.assertIn("'settled_quote'", _sql)
        self.assertEqual(params['effective_at'].date(), date(2026,8,20))
        self.assertFalse(any('enabled=false' in sql or 'DELETE FROM quant.universe_membership_history' in sql
                             or 'closed_by' in sql for sql, _ in connection.calls))

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

    def test_no_adjustment_factor_is_ever_persisted_for_this_vendor(self) -> None:
        """The vendor supplies no corporate-action history.

        The identity placeholder it used to write was promoted onto
        ``quant.canonical_bars_daily.adj_factor`` exactly like a real
        cumulative tushare factor, so every cross-date price adjustment on a
        vendor day silently produced an unadjusted series.
        """
        connection = _RecordingConnection(fetch_run_id="run-1")
        persisted: list[str] = []

        def persist_rows(_connection, api_name, *_args, **_kwargs):
            persisted.append(api_name)
            return 0

        result = persist_full_market_close(
            connection,
            trade_date=date(2026, 9, 18),
            request_key="req-1",
            observed_at=datetime(2026, 9, 18, 7, tzinfo=timezone.utc),
            merged=MergedCrossSection(
                daily_rows=[{"ts_code": "600664.SH", "trade_date": "20260918", "pre_close": 10,
                             "name": "test"}],
                fundamental_rows=[], flow_rows=[], quote_rows=[], coverage=1.0, close_conflicts=(),
            ),
            source_health={},
            board_rows=[],
            persist_rows=persist_rows,
            persist_flow_rows=lambda *_a, **_k: 0,
        )
        self.assertNotIn("adj_factor", persisted)
        self.assertIn("stk_limit", persisted)
        self.assertNotIn("adj_factor", result.get("normalized", {}))
        receipt = [params for sql, params in connection.calls
                   if "UPDATE quant.fetch_runs SET status='completed'" in sql][0]
        semantics = receipt[1].obj["control_semantics"]
        self.assertNotEqual(semantics["adj_factor"], "same_day_identity_only")
        self.assertIn("not_supplied_by_vendor", semantics["adj_factor"])

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

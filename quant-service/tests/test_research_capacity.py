from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from app.research_capacity import feature_readiness_projection, feature_readiness_state


class FeatureReadinessProjectionTests(unittest.TestCase):
    def test_partial_enrichment_does_not_block_complete_p0_daily_baseline(self):
        result = feature_readiness_projection([
            {"feature": "daily_bars", "symbols": 1_000, "rows": 10_000},
            {"feature": "daily_basic", "symbols": 1_000, "rows": 10_000},
            {"feature": "trade_limits", "symbols": 1_000, "rows": 10_000},
            {"feature": "moneyflow", "symbols": 5, "rows": 50},
            {"feature": "announcements", "symbols": 0, "rows": 0},
        ], 1_000)
        self.assertTrue(result["decision_ready"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["supplementary_partial"], ["moneyflow", "announcements"])

    def test_missing_required_daily_input_remains_hard_blocker(self):
        result = feature_readiness_projection([
            {"feature": "daily_bars", "symbols": 1_000, "rows": 10_000},
            {"feature": "daily_basic", "symbols": 799, "rows": 10_000},
            {"feature": "trade_limits", "symbols": 1_000, "rows": 10_000},
        ], 1_000)
        self.assertFalse(result["decision_ready"])
        self.assertEqual(result["blockers"], ["daily_basic"])

    def test_cross_section_sql_requires_eighty_percent_of_the_live_universe(self):
        source = Path("app/research_capacity.py").read_text(encoding="utf-8")
        self.assertIn("greatest(ceil(universe.symbols*0.8)::int,1000)", source)
        self.assertNotIn("least(universe.symbols*0.8,1000)", source)


class _SyncResult:
    def __init__(self, *, row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._row, self._rows = row, rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _SyncConnection:
    """Fake synchronous connection matching ``connection.execute(sql, params)``.

    Mirrors ``tests/test_feature_readiness_estimated.py``'s async fake
    connection so both readers of the shared feature-readiness SQL are
    verified with the same fixture shape.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _SyncResult:
        normalized = " ".join(sql.split())
        self.calls.append(normalized)
        if "table_estimates AS" in normalized:
            return _SyncResult(rows=[
                {"feature": "daily_bars", "symbols": 5000, "rows": 1200000, "latest_date": "2026-08-20", "priority": "P0"},
                {"feature": "daily_basic", "symbols": 5000, "rows": 1200000, "latest_date": "2026-08-20", "priority": "P0"},
                {"feature": "trade_limits", "symbols": 5000, "rows": 1200000, "latest_date": "2026-08-20", "priority": "P0"},
                {"feature": "sector_flow", "symbols": 0, "rows": 50000, "latest_date": "2026-08-20", "priority": "P1"},
                {"feature": "announcements", "symbols": 0, "rows": 900, "latest_date": "2026-08-20", "priority": "P1"},
                {"feature": "analyst_claims", "symbols": 0, "rows": 300, "latest_date": "2026-08-20", "priority": "P1"},
            ])
        if "FROM quant.tushare_raw_records WHERE api_name=ANY" in normalized:
            return _SyncResult(rows=[{"api_name": "moneyflow_dc", "symbols": 4000, "rows": 4000, "latest_date": "2026-08-20"}])
        if "FROM quant.universe_members WHERE universe_key='all_a'" in normalized:
            return _SyncResult(row={"symbols": 5000})
        raise AssertionError(f"unexpected SQL: {normalized}")


class FeatureReadinessStateTests(unittest.TestCase):
    """``feature_readiness_state`` must reuse the async pg_stat estimate SQL.

    Before this fix it ran its own byte-identical unbounded ``count(DISTINCT
    ...)``/``count(*)`` scan of the same tables as
    ``async_market_result_read_repository.feature_readiness_estimated``; now
    both execute ``FEATURE_READINESS_ESTIMATE_SQL`` verbatim and the response
    is marked ``estimated``.
    """

    def test_response_is_marked_estimated_and_uses_one_table_estimate_query(self) -> None:
        connection = _SyncConnection()
        payload = feature_readiness_state(connection)
        self.assertTrue(payload["estimated"])
        self.assertTrue(payload["decision_ready"])
        self.assertEqual(payload["universe_symbols"], 5000)
        features = {item["feature"] for item in payload["items"]}
        self.assertIn("daily_bars", features)
        self.assertIn("moneyflow_dc", features)
        table_scans = [sql for sql in connection.calls if "table_estimates AS" in sql]
        self.assertEqual(len(table_scans), 1)

    def test_missing_moneyflow_dc_rows_report_zero_not_an_error(self) -> None:
        connection = _SyncConnection()
        original_execute = connection.execute

        def patched(sql: str, params: tuple[Any, ...] | None = None) -> _SyncResult:
            normalized = " ".join(sql.split())
            if "FROM quant.tushare_raw_records WHERE api_name=ANY" in normalized:
                connection.calls.append(normalized)
                return _SyncResult(rows=[])
            return original_execute(sql, params)

        connection.execute = patched
        payload = feature_readiness_state(connection)
        moneyflow = next(item for item in payload["items"] if item["feature"] == "moneyflow_dc")
        self.assertEqual(moneyflow["symbols"], 0)
        self.assertEqual(moneyflow["status"], "missing")

    def test_uses_the_same_sql_text_as_the_async_estimate(self) -> None:
        from app.async_market_result_read_repository import FEATURE_READINESS_ESTIMATE_SQL

        connection = _SyncConnection()
        feature_readiness_state(connection)
        self.assertIn(" ".join(FEATURE_READINESS_ESTIMATE_SQL.split()), connection.calls)
        source = Path("app/research_capacity.py").read_text(encoding="utf-8")
        self.assertNotIn("count(DISTINCT symbol)::int symbols,count(*)::int rows,max(trading_date)", source)


if __name__ == "__main__":
    unittest.main()

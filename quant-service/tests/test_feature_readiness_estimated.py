"""Coverage for the consolidated pg_stat-based feature-readiness projection.

``async_research_readiness_repository.feature_readiness`` previously ran its
own unbounded ``count(DISTINCT ...)``/``count(*)`` scan of the largest
control-plane tables -- a byte-for-byte duplicate of the same query pattern
that already existed (with pg_stat estimates instead) in
``async_market_result_read_repository``.  Both now share one implementation,
``feature_readiness_estimated``, and its response is marked ``estimated``.
"""

from __future__ import annotations

import unittest

from app.async_market_result_read_repository import feature_readiness_estimated
from app.async_research_readiness_repository import feature_readiness


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row, self._rows = row, rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append(normalized)
        if "table_estimates AS" in normalized:
            return _Result(rows=[
                {"feature": "daily_bars", "symbols": 5000, "rows": 1200000, "latest_date": "2026-08-20", "priority": "P0"},
                {"feature": "daily_basic", "symbols": 5000, "rows": 1200000, "latest_date": "2026-08-20", "priority": "P0"},
                {"feature": "trade_limits", "symbols": 5000, "rows": 1200000, "latest_date": "2026-08-20", "priority": "P0"},
                {"feature": "sector_flow", "symbols": 0, "rows": 50000, "latest_date": "2026-08-20", "priority": "P1"},
                {"feature": "announcements", "symbols": 0, "rows": 900, "latest_date": "2026-08-20", "priority": "P1"},
                {"feature": "analyst_claims", "symbols": 0, "rows": 300, "latest_date": "2026-08-20", "priority": "P1"},
            ])
        if "FROM quant.tushare_raw_records WHERE api_name=ANY" in normalized:
            return _Result(rows=[{"api_name": "moneyflow_dc", "symbols": 4000, "rows": 4000, "latest_date": "2026-08-20"}])
        if "FROM quant.universe_members WHERE universe_key='all_a'" in normalized:
            return _Result(row={"symbols": 5000})
        raise AssertionError(f"unexpected SQL: {normalized}")


class FeatureReadinessEstimatedTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_is_marked_estimated_and_uses_one_table_estimate_query(self) -> None:
        connection = _Connection()
        payload = await feature_readiness_estimated(connection)
        self.assertTrue(payload["estimated"])
        self.assertTrue(payload["decision_ready"])
        self.assertEqual(payload["universe_symbols"], 5000)
        features = {item["feature"] for item in payload["items"]}
        self.assertIn("daily_bars", features)
        self.assertIn("moneyflow_dc", features)
        # Only two large-table scans regardless of feature count: the pg_stat
        # CTE query and the indexed per-api_name tushare_raw_records query.
        table_scans = [sql for sql in connection.calls if "table_estimates AS" in sql]
        self.assertEqual(len(table_scans), 1)

    async def test_missing_moneyflow_dc_rows_report_zero_not_an_error(self) -> None:
        connection = _Connection()
        connection.execute = _no_moneyflow_execute(connection)
        payload = await feature_readiness_estimated(connection)
        moneyflow = next(item for item in payload["items"] if item["feature"] == "moneyflow_dc")
        self.assertEqual(moneyflow["symbols"], 0)
        self.assertEqual(moneyflow["status"], "missing")


def _no_moneyflow_execute(connection: _Connection):
    original = _Connection.execute

    async def patched(sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM quant.tushare_raw_records WHERE api_name=ANY" in normalized:
            connection.calls.append(normalized)
            return _Result(rows=[])
        return await original(connection, sql, params)

    return patched


class AsyncResearchReadinessFeatureReadinessDelegatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_feature_readiness_delegates_to_the_shared_estimate(self) -> None:
        connection = _Connection()

        class _Transaction:
            async def __aenter__(self_inner):
                return connection

            async def __aexit__(self_inner, *_args):
                return False

        class _Database:
            def transaction(self_inner):
                return _Transaction()

        payload = await feature_readiness(_Database())
        self.assertTrue(payload["estimated"])
        # The old implementation ran its own inline count(DISTINCT ...)/count(*)
        # UNION ALL; that text must no longer appear anywhere in what was sent.
        self.assertFalse(any("count(DISTINCT symbol)::int rows" in sql for sql in connection.calls))


if __name__ == "__main__":
    unittest.main()

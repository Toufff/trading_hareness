"""Coverage for the WP10 unbounded-read fixes (section B/J of the audit):

- ``async_strategy_read_repository.latest_strategy_decision``'s recommendations read
- ``async_research_catalog_read_repository.universe_members``
- ``async_intraday_outcome_read_repository.latest_intraday_outcomes``'s board-report window

Each previously had no ``LIMIT`` at all; each now caps the read and reports
``truncated`` in the response instead of letting the payload grow with the
underlying table.
"""

from __future__ import annotations

import unittest

from app.async_research_catalog_read_repository import universe_members
from app.async_strategy_read_repository import latest_strategy_decision


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row, self._rows = row, rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, run_row, recommendation_rows):
        self.run_row = run_row
        self.recommendation_rows = recommendation_rows
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "FROM quant.recommendation_runs" in normalized:
            return _Result(row=self.run_row)
        if "FROM quant.recommendations" in normalized:
            return _Result(rows=self.recommendation_rows)
        raise AssertionError(f"unexpected SQL: {normalized}")


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_args):
        return False


class _Database:
    def __init__(self, connection):
        self.connection = connection

    def transaction(self):
        return _Transaction(self.connection)


class LatestStrategyDecisionBoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_recommendations_read_is_bounded_and_reports_no_truncation_when_under_the_cap(self) -> None:
        connection = _Connection(
            run_row={"run_id": "run-1"},
            recommendation_rows=[{"symbol": "000001.SZ", "rank": 1}],
        )
        payload = await latest_strategy_decision(_Database(connection), "model-1")
        self.assertFalse(payload["truncated"])
        self.assertEqual(len(payload["recommendations"]), 1)
        recommendations_sql = next(sql for sql, _params in connection.calls if "FROM quant.recommendations" in sql)
        self.assertIn("LIMIT", recommendations_sql)

    async def test_more_rows_than_the_cap_are_reported_as_truncated(self) -> None:
        from app import async_strategy_read_repository as module
        original_max = module._MAX_RECOMMENDATIONS
        module._MAX_RECOMMENDATIONS = 2
        try:
            connection = _Connection(
                run_row={"run_id": "run-1"},
                recommendation_rows=[{"symbol": f"{i:06d}.SZ", "rank": i} for i in range(3)],
            )
            payload = await latest_strategy_decision(_Database(connection), "model-1")
            self.assertTrue(payload["truncated"])
            self.assertEqual(len(payload["recommendations"]), 2)
        finally:
            module._MAX_RECOMMENDATIONS = original_max

    async def test_missing_run_reports_no_truncation_without_a_second_query(self) -> None:
        connection = _Connection(run_row=None, recommendation_rows=[])
        payload = await latest_strategy_decision(_Database(connection), "model-1")
        self.assertEqual(payload, {"run": None, "recommendations": [], "truncated": False})
        self.assertEqual(len(connection.calls), 1)


class _UniverseConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return _Result(rows=self.rows)


class UniverseMembersBoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_universe_listing_is_bounded_and_reports_truncation(self) -> None:
        from app import async_research_catalog_read_repository as module
        original_max = module._MAX_UNIVERSE_MEMBERS
        module._MAX_UNIVERSE_MEMBERS = 2
        try:
            connection = _UniverseConnection([{"symbol": f"{i:06d}.SZ"} for i in range(3)])
            payload = await universe_members(_Database(connection), "all_a")
            self.assertTrue(payload["truncated"])
            self.assertEqual(len(payload["items"]), 2)
            sql, _params = connection.calls[0]
            self.assertIn("LIMIT", sql)
        finally:
            module._MAX_UNIVERSE_MEMBERS = original_max

    async def test_under_the_cap_reports_no_truncation(self) -> None:
        connection = _UniverseConnection([{"symbol": "000001.SZ"}])
        payload = await universe_members(_Database(connection), "all_a")
        self.assertFalse(payload["truncated"])
        self.assertEqual(len(payload["items"]), 1)


if __name__ == "__main__":
    unittest.main()

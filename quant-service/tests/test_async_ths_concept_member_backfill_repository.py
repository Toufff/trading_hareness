from __future__ import annotations

from datetime import date
import unittest

from app.async_ths_concept_member_backfill_repository import existing_flow_rows, member_progress


class _Result:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        if "sector_market_observations" in query:
            return _Result({"rows": 4})
        return _Result({"done": 3, "failed": 1})


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class _Database:
    def __init__(self):
        self.connection = _Connection()

    def transaction(self):
        return _Transaction(self.connection)


class AsyncThsConceptMemberBackfillRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_same_day_flow_and_exact_member_progress(self) -> None:
        database = _Database()
        trade_date = date(2026, 8, 21)

        existing = await existing_flow_rows(database, trade_date)
        progress = await member_progress(database, trade_date)

        self.assertEqual(existing, {"rows": 4})
        self.assertEqual(progress, {"done": 3, "failed": 1})
        flow_query, flow_params = database.connection.calls[0]
        progress_query, progress_params = database.connection.calls[1]
        self.assertIn("taxonomy_key='ths_concept_flow'", flow_query)
        self.assertEqual(flow_params, (trade_date,))
        self.assertIn("state IN ('completed','empty')", progress_query)
        self.assertEqual(progress_params, (trade_date,))


if __name__ == "__main__":
    unittest.main()

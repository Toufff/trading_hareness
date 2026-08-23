from __future__ import annotations

from datetime import date
import unittest

from app.async_limit_linkage_relation_repository import relations


class _Result:
    async def fetchall(self):
        return [{
            "symbol": "000001.SZ", "concept_keys": ["885001.TI", "885002.TI"],
            "concept_labels": ["测试概念"], "leader_symbols": ["000002.SZ"], "leader_names": ["领涨股"],
        }]


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        return _Result()


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


class AsyncLimitLinkageRelationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_same_day_limit_anchors_and_exact_active_ths_memberships(self) -> None:
        database = _Database()
        trade_date = date(2026, 8, 21)

        rows = await relations(database, trade_date)

        self.assertEqual(rows[0]["shared_concepts"], 2)
        query, params = database.connection.calls[0]
        self.assertIn("event.event_type='limit_up_pool'", query)
        self.assertIn("taxonomy_key='ths_concept_flow'", query)
        self.assertIn("HAVING count(*) BETWEEN 2 AND 200", query)
        self.assertEqual(params, (trade_date,))


if __name__ == "__main__":
    unittest.main()

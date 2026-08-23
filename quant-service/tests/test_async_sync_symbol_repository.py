from __future__ import annotations

import unittest

from app.async_sync_symbol_repository import analyst_claim_symbols, core_symbols


class _Result:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params=None):
        self.calls.append((query, params))
        if "universe_members" in query:
            return _Result([{"symbol": "000001.SZ"}, {"symbol": "600519.SH"}])
        return _Result([{"subject_key": "300750.SZ"}])


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


class AsyncSyncSymbolRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_enabled_core_before_exact_stock_claim_fallback(self) -> None:
        database = _Database()

        core = await core_symbols(database)
        claims = await analyst_claim_symbols(database)

        self.assertEqual(core, ["000001.SZ", "600519.SH"])
        self.assertEqual(claims, ["300750.SZ"])
        core_query, core_params = database.connection.calls[0]
        claims_query, claims_params = database.connection.calls[1]
        self.assertIn("universe_key='core' AND enabled", core_query)
        self.assertIsNone(core_params)
        self.assertIn("scope='stock'", claims_query)
        self.assertIn("subject_key ~", claims_query)
        self.assertIsNone(claims_params)


if __name__ == "__main__":
    unittest.main()

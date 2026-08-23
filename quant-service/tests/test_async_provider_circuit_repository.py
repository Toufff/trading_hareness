from __future__ import annotations

import unittest

from app.async_provider_circuit_repository import open_capabilities, open_provider_keys


class _Result:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        if "SELECT capability" in query:
            return _Result([{"capability": "realtime_quote"}])
        return _Result([{"provider_key": "tushare_super_get"}])


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


class AsyncProviderCircuitRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_async_circuit_reads_are_bounded_and_empty_safe(self) -> None:
        database = _Database()
        self.assertEqual(await open_capabilities(database, "tencent_free", ["realtime_quote"]), {"realtime_quote"})
        self.assertEqual(await open_provider_keys(database, "daily", ["tushare_super_get"]), {"tushare_super_get"})
        self.assertEqual(await open_capabilities(database, "tencent_free", []), set())
        self.assertEqual(await open_provider_keys(database, "daily", []), set())
        capabilities_query, capabilities_params = database.connection.calls[0]
        providers_query, providers_params = database.connection.calls[1]
        self.assertIn("circuit_open_until > now()", capabilities_query)
        self.assertEqual(capabilities_params, ("tencent_free", ["realtime_quote"]))
        self.assertIn("circuit_open_until > now()", providers_query)
        self.assertEqual(providers_params, ("daily", ["tushare_super_get"]))


if __name__ == "__main__":
    unittest.main()

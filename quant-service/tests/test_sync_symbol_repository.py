"""Synchronous fallback-universe repository coverage."""

from __future__ import annotations

import unittest

from app.sync_symbol_repository import analyst_claim_symbols, core_symbols


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if "universe_members" in statement:
            return _Result([{"symbol": "600519.SH"}, {"symbol": "000001.SZ"}])
        return _Result([{"subject_key": "300750.SZ"}])


class SyncSymbolRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = _Connection()
        self.database = type("Database", (), {"transaction": lambda _self: _Transaction(self.connection)})()

    def test_core_symbols_reads_enabled_priority_ordered_universe(self):
        self.assertEqual(core_symbols(self.database), ["600519.SH", "000001.SZ"])
        self.assertIn("WHERE universe_key='core' AND enabled", self.connection.statements[-1])
        self.assertIn("ORDER BY priority,symbol", self.connection.statements[-1])

    def test_analyst_claim_symbols_keeps_stock_scope_and_symbol_guard(self):
        self.assertEqual(analyst_claim_symbols(self.database), ["300750.SZ"])
        self.assertIn("scope='stock'", self.connection.statements[-1])
        self.assertIn("subject_key ~", self.connection.statements[-1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

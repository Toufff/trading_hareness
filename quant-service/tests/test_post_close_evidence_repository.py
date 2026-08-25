"""Database-boundary coverage for persisted post-close evidence reads."""

from __future__ import annotations

from datetime import date
import unittest

from app.post_close_evidence_repository import (
    load_exact_board_context_rows,
    load_tushare_lhb_context_rows,
)


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
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        return _Result([{"symbol": "000001.SZ", "row_data": {"trade_date": "20260824"}}])


class PostCloseEvidenceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = _Connection()
        self.database = type("Database", (), {"transaction": lambda _self: _Transaction(self.connection)})()
        self.as_of_date = date(2026, 8, 24)

    def test_exact_board_read_is_same_date_and_returns_plain_rows(self):
        rows = load_exact_board_context_rows(self.database, self.as_of_date)

        self.assertEqual(rows, [{"symbol": "000001.SZ", "row_data": {"trade_date": "20260824"}}])
        statement, params = self.connection.calls[-1]
        self.assertIn("quant.sector_membership_history", statement)
        self.assertIn("flow.trading_date=%s", statement)
        self.assertEqual(params, (self.as_of_date,))

    def test_lhb_read_uses_tushare_trade_date_format(self):
        load_tushare_lhb_context_rows(self.database, self.as_of_date)

        statement, params = self.connection.calls[-1]
        self.assertIn("api_name IN ('top_list','top_inst')", statement)
        self.assertEqual(params, ("20260824",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

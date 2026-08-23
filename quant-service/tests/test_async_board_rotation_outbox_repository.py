from __future__ import annotations

import unittest

from app.async_board_rotation_outbox_repository import suppress_legacy_deliveries


class _Result:
    rowcount = 2


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params=None):
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


class AsyncBoardRotationOutboxRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_suppresses_only_legacy_unsent_feishu_rows(self) -> None:
        database = _Database()

        suppressed = await suppress_legacy_deliveries(database)

        self.assertEqual(suppressed, 2)
        query, params = database.connection.calls[0]
        self.assertIn("status='suppressed'", query)
        self.assertIn("channel='feishu_adapter' AND status IN ('pending','failed')", query)
        self.assertIsNone(params)


if __name__ == "__main__":
    unittest.main()

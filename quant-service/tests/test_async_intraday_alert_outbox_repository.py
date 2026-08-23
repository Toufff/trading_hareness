from __future__ import annotations

import uuid
import unittest

from app.async_intraday_alert_outbox_repository import create_pending, due_deliveries


class _Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []

    async def fetchone(self):
        return self.one

    async def fetchall(self):
        return self.many


class _Connection:
    def __init__(self, delivery_id):
        self.delivery_id = delivery_id
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        if query.lstrip().startswith("INSERT"):
            return _Result(one={"delivery_id": self.delivery_id})
        return _Result(many=[{
            "delivery_id": self.delivery_id, "signal_event_id": uuid.uuid4(), "message_text": "retry",
        }])


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class _Database:
    def __init__(self):
        self.delivery_id = uuid.uuid4()
        self.connection = _Connection(self.delivery_id)

    def transaction(self):
        return _Transaction(self.connection)


class AsyncIntradayAlertOutboxRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_before_send_and_bounds_due_retry_rows(self) -> None:
        database = _Database()
        signal_event_id = uuid.uuid4()

        delivery_id = await create_pending(database, signal_event_id, "signal")
        due = await due_deliveries(database, max_attempts=3, limit=99)

        self.assertEqual(delivery_id, database.delivery_id)
        self.assertEqual(len(due), 1)
        insert_query, insert_params = database.connection.calls[0]
        due_query, due_params = database.connection.calls[1]
        self.assertIn("VALUES(%s,'feishu_adapter','pending',%s,now())", insert_query)
        self.assertEqual(insert_params, (signal_event_id, "signal"))
        self.assertIn("NOT EXISTS", due_query)
        self.assertEqual(due_params, (3, 10))


if __name__ == "__main__":
    unittest.main()

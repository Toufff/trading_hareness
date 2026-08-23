from __future__ import annotations

import asyncio
import unittest
import uuid
from pathlib import Path

from app.intraday_alert_delivery_service import attempt_delivery, load_due_deliveries


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return _Cursor([{"delivery_id": uuid.uuid4(), "signal_event_id": uuid.uuid4(), "message_text": "retry"}])


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _Database:
    def __init__(self):
        self.connection = _Connection()

    def transaction(self):
        return _Transaction(self.connection)


class IntradayAlertDeliveryServiceTests(unittest.TestCase):
    def test_due_rows_are_bounded_and_exclude_already_sent_events(self) -> None:
        database = _Database()
        rows = load_due_deliveries(database, max_attempts=3, limit=99)
        self.assertEqual(len(rows), 1)
        statement, params = database.connection.calls[0]
        self.assertIn("NOT EXISTS", statement)
        self.assertIn("attempt_count<%s", statement)
        self.assertEqual(params, (3, 10))

    def test_recovery_receipt_uses_the_injected_transport_after_normal_message(self) -> None:
        health_event = {"health_event_id": uuid.uuid4(), "event_type": "recovered", "message_text": "recovered"}

        async def check():
            sent: list[str] = []

            async def post_text(text: str):
                sent.append(text)
                return {"status": "sent", "response": {"ok": True}}

            calls = 0

            async def run_database(_operation):
                nonlocal calls
                calls += 1
                return health_event if calls == 1 else None

            outcome = await attempt_delivery(
                object(), uuid.uuid4(), uuid.uuid4(), "signal", post_text=post_text,
                run_database=run_database, json_safe=lambda value: value,
                recovery_text=lambda streak: f"recovery {streak}", max_attempts=3,
            )
            return outcome, sent, calls

        outcome, sent, calls = asyncio.run(check())
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(sent, ["signal", "recovered"])
        self.assertEqual(calls, 2)

    def test_service_has_no_main_or_http_client_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_alert_delivery_service.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("def load_due_deliveries", source)


if __name__ == "__main__":
    unittest.main()

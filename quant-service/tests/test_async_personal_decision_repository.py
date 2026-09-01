"""Freshness behavior for the native-async personal decision read model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.async_personal_decision_repository import latest_market_section


NOW = datetime(2026, 9, 1, 7, 15, tzinfo=timezone.utc)


class Result:
    def __init__(self, row): self.row = row
    async def fetchone(self): return self.row


class Connection:
    def __init__(self, row): self.row = row
    async def execute(self, _sql, _params=()): return Result(self.row)


class Transaction:
    def __init__(self, row): self.connection = Connection(row)
    async def __aenter__(self): return self.connection
    async def __aexit__(self, *_args): return False


class Database:
    def __init__(self, row): self.row = row
    def transaction(self): return Transaction(self.row)


class AsyncPersonalDecisionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_section_has_an_explicit_age_boundary(self) -> None:
        base = {
            "review_id": "review-1", "exchange_date": NOW.date(), "session": "close",
            "market_state": "rotation", "data_boundary": {}, "report": {},
        }
        current = await latest_market_section(
            Database(base | {"observed_at": NOW - timedelta(hours=66)}), as_of_at=NOW,
        )
        self.assertEqual(current["status"], "ready")

        stale = await latest_market_section(
            Database(base | {"observed_at": NOW - timedelta(days=5)}), as_of_at=NOW,
        )
        self.assertEqual(stale["status"], "unavailable")

        future = await latest_market_section(
            Database(base | {"observed_at": NOW + timedelta(hours=1)}), as_of_at=NOW,
        )
        self.assertEqual(future["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

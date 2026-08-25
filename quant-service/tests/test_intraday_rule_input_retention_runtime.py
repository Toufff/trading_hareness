"""Regression coverage for once-per-exchange-date intraday evidence retention."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from app.intraday_rule_input_retention_runtime import (
    IntradayRuleInputRetentionDependencies,
    IntradayRuleInputRetentionRuntime,
)


class _Transaction:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    def __enter__(self) -> object:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.connection = object()
        self.transactions = 0

    def transaction(self) -> _Transaction:
        self.transactions += 1
        return _Transaction(self.connection)


class IntradayRuleInputRetentionRuntimeTests(unittest.TestCase):
    def test_prunes_once_for_one_shanghai_exchange_date_with_separate_cutoffs(self):
        database = _Database()
        calls: list[tuple[str, object, datetime]] = []

        async def run_database(operation):
            return operation()

        runtime = IntradayRuleInputRetentionRuntime(IntradayRuleInputRetentionDependencies(
            database=database,
            run_database=run_database,
            rule_input_retention_days=lambda: 14,
            ephemeral_signal_retention_days=lambda: 5,
            prune_rule_inputs=lambda connection, *, cutoff: calls.append(("rule", connection, cutoff)),
            prune_ephemeral_events=lambda connection, *, cutoff: calls.append(("event", connection, cutoff)),
        ))
        observed_at = datetime(2026, 8, 24, 2, 30, tzinfo=timezone.utc)

        asyncio.run(runtime.prune_if_due(observed_at))
        asyncio.run(runtime.prune_if_due(observed_at + timedelta(hours=1)))

        self.assertEqual(database.transactions, 1)
        self.assertEqual([name for name, _, _ in calls], ["rule", "event"])
        self.assertEqual(calls[0][1], database.connection)
        self.assertEqual(calls[0][2], observed_at - timedelta(days=14))
        self.assertEqual(calls[1][2], observed_at - timedelta(days=5))


if __name__ == "__main__":
    unittest.main()

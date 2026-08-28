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


class EdgeChangeJournalRetentionTests(unittest.TestCase):
    """The delivery journal has to be bounded by the same daily pass.

    Nothing pruned it before: it reached 468k rows and 43% of the edge
    database on 2026-08-28, which took the hot budget to 99% and made the
    storage guard stop non-essential capture for a session. The guard bounds
    what is written; only retention returns what was written.
    """

    def _run(self, *, prune_journal=None, retention_days=None, observed_at=None):
        database = _Database()
        calls: list[tuple[str, datetime]] = []

        async def run_database(operation):
            return operation()

        runtime = IntradayRuleInputRetentionRuntime(IntradayRuleInputRetentionDependencies(
            database=database,
            run_database=run_database,
            rule_input_retention_days=lambda: 14,
            ephemeral_signal_retention_days=lambda: 5,
            prune_rule_inputs=lambda connection, *, cutoff: calls.append(("rule", cutoff)),
            prune_ephemeral_events=lambda connection, *, cutoff: calls.append(("event", cutoff)),
            prune_change_journal=(
                (lambda connection, *, cutoff: calls.append(("journal", cutoff)))
                if prune_journal is None else prune_journal),
            change_journal_retention_days=retention_days,
        ))
        stamp = observed_at or datetime(2026, 8, 28, 2, 30, tzinfo=timezone.utc)
        asyncio.run(runtime.prune_if_due(stamp))
        return calls, stamp

    def test_the_journal_is_pruned_in_the_same_pass(self):
        calls, observed_at = self._run(retention_days=lambda: 3)
        self.assertEqual([name for name, _ in calls], ["rule", "event", "journal"])
        self.assertEqual(calls[2][1], observed_at - timedelta(days=3))

    def test_the_journal_prune_runs_last(self):
        # The evidence prunes are the ones that must happen; the journal is a
        # delivery log. Ordering it last keeps a journal failure from being
        # able to take the retention that matters down with it.
        calls, _ = self._run(retention_days=lambda: 3)
        self.assertEqual([name for name, _ in calls][-1], "journal")

    def test_an_unconfigured_window_leaves_the_journal_alone(self):
        calls, _ = self._run(retention_days=None)
        self.assertEqual([name for name, _ in calls], ["rule", "event"])

    def test_a_zero_day_window_is_treated_as_disabled_not_as_delete_everything(self):
        calls, _ = self._run(retention_days=lambda: 0)
        self.assertEqual([name for name, _ in calls], ["rule", "event"])

    def test_a_call_site_that_predates_the_journal_still_constructs(self):
        # The two new dependencies carry defaults so an existing caller that
        # knows nothing about the journal keeps its original behaviour rather
        # than failing to build.
        database = _Database()
        calls: list[str] = []

        async def run_database(operation):
            return operation()

        runtime = IntradayRuleInputRetentionRuntime(IntradayRuleInputRetentionDependencies(
            database=database,
            run_database=run_database,
            rule_input_retention_days=lambda: 14,
            ephemeral_signal_retention_days=lambda: 5,
            prune_rule_inputs=lambda connection, *, cutoff: calls.append("rule"),
            prune_ephemeral_events=lambda connection, *, cutoff: calls.append("event"),
        ))
        asyncio.run(runtime.prune_if_due(datetime(2026, 8, 28, 2, 30, tzinfo=timezone.utc)))
        self.assertEqual(calls, ["rule", "event"])


class ChangeJournalPruneQueryTests(unittest.TestCase):
    """The pruner itself must be safe on a database that has no journal."""

    class _Connection:
        def __init__(self, table: object) -> None:
            self._table = table
            self.statements: list[str] = []
            self.rowcount = 7

        def execute(self, sql, parameters=None):
            self.statements.append(" ".join(sql.split()))
            if "to_regclass" in sql:
                return _Fetch({"value": self._table})
            self.last_parameters = parameters
            return self

        def fetchone(self):
            return {"value": self._table}

    def test_a_missing_journal_returns_zero_without_deleting(self):
        from app.edge_evidence_transfer import prune_change_journal
        connection = self._Connection(None)
        deleted = prune_change_journal(
            connection, cutoff=datetime(2026, 8, 25, tzinfo=timezone.utc))
        self.assertEqual(deleted, 0)
        self.assertNotIn(True, ["DELETE" in statement for statement in connection.statements])

    def test_an_existing_journal_deletes_by_age(self):
        from app.edge_evidence_transfer import prune_change_journal
        connection = self._Connection("quant.edge_evidence_changes")
        cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
        deleted = prune_change_journal(connection, cutoff=cutoff)
        self.assertEqual(deleted, 7)
        delete = next(s for s in connection.statements if s.startswith("DELETE"))
        self.assertIn("changed_at < %s", delete)
        self.assertNotIn("sequence_id", delete)
        self.assertEqual(connection.last_parameters, (cutoff,))

    def test_a_known_cursor_adds_a_sequence_floor_below_the_replay_window(self):
        from app.edge_evidence_transfer import CHANGE_REPLAY_WINDOW, prune_change_journal
        connection = self._Connection("quant.edge_evidence_changes")
        cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
        prune_change_journal(connection, cutoff=cutoff, keep_after_sequence=500_000)
        delete = next(s for s in connection.statements if s.startswith("DELETE"))
        self.assertIn("sequence_id <= %s", delete)
        self.assertEqual(connection.last_parameters,
                         (cutoff, 500_000 - CHANGE_REPLAY_WINDOW))


class _Fetch:
    def __init__(self, row: dict) -> None:
        self._row = row

    def fetchone(self) -> dict:
        return self._row


if __name__ == "__main__":
    unittest.main()

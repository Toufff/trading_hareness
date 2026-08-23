from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.research_maintenance_service import (
    ResearchMaintenanceDependencies,
    reconcile_stale_fetch_runs,
    update_analyst_profile,
    update_universe_members,
)


class _Transaction:
    def __init__(self, execute):
        self.execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _deps(database: MagicMock, **overrides) -> ResearchMaintenanceDependencies:
    values = {
        "database": database,
        "china_today": lambda: date(2026, 8, 21),
        "exchange_for": lambda symbol: symbol.rsplit(".", 1)[-1],
        "rebuild_analyst_research": MagicMock(return_value={"sleeping_experts": {"status": "research_only"}}),
        "sync_universe_membership_history": MagicMock(return_value={"changed": True}),
        "http_exception": HTTPException,
        "now_utc": lambda: datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return ResearchMaintenanceDependencies(**values)


class ResearchMaintenanceServiceTests(unittest.TestCase):
    def test_missing_analyst_profile_is_rejected_before_rebuild(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        database = MagicMock()
        database.transaction.return_value = _Transaction(connection.execute)
        rebuild = MagicMock()
        payload = SimpleNamespace(independence_class="unknown", audience_size=None, audience_as_of=None, evidence={})

        with self.assertRaises(HTTPException) as raised:
            update_analyst_profile("missing", payload, _deps(database, rebuild_analyst_research=rebuild))

        self.assertEqual(raised.exception.status_code, 404)
        rebuild.assert_not_called()

    def test_universe_update_records_the_full_active_point_in_time_set(self) -> None:
        statements = []

        def execute(sql, params=()):
            statements.append((str(sql), params))
            if "SELECT symbol FROM quant.universe_members" in str(sql):
                return MagicMock(fetchall=MagicMock(return_value=[{"symbol": "000001.SZ"}, {"symbol": "600000.SH"}]))
            return MagicMock()

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)
        sync_history = MagicMock(return_value={"changed": True})
        payload = SimpleNamespace(universe_key="core", symbols=["000001.SZ"], enabled=True, priority=5)

        result = update_universe_members(payload, _deps(database, sync_universe_membership_history=sync_history))

        self.assertEqual(result["updated"], 1)
        self.assertEqual(sync_history.call_args.args[3], ["000001.SZ", "600000.SH"])
        self.assertEqual(sync_history.call_args.kwargs["source"], "universe-members-api")

    def test_stale_fetch_dry_run_never_updates_rows(self) -> None:
        statements = []

        def execute(sql, params=()):
            statements.append((str(sql), params))
            return MagicMock(fetchall=MagicMock(return_value=[{"request_key": "stale", "status": "running"}]))

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)
        payload = SimpleNamespace(max_age_minutes=10, dry_run=True, terminal_status="failed")

        result = reconcile_stale_fetch_runs(payload, _deps(database))

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["matched"], 1)
        self.assertEqual(len(statements), 1)
        self.assertIn("status='running'", statements[0][0])

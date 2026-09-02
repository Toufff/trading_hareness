"""WP6 reaper follow-up: the automated daily retention-maintenance task.

Audit (section B) + wp7-report.md's suggestion: quant.retention_policies and
quant.apply_retention_policy already exist (migration 20260902_0085) but
nothing ever calls the function automatically. This wires one leased,
off-by-default daily BackgroundTaskSpec that applies every *enabled* policy's
bounded batches, deduplicated through automation_run_repository.run_recorded.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import app.main as main
from app.platform.runtime_task_registry import RUNTIME_TASK_CONTRACTS


class _Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _FakeDatabase:
    def __init__(self, execute) -> None:
        self._execute = execute

    def transaction(self):
        return _Transaction(_FakeConnection(self._execute))


class _FakeConnection:
    def __init__(self, execute) -> None:
        self._execute = execute

    def execute(self, sql, params=()):
        return self._execute(sql, params)


class RetentionMaintenanceAutomationEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUANT_RETENTION_MAINTENANCE_AUTOMATION_ENABLED", None)
            self.assertFalse(main.retention_maintenance_automation_enabled())

    def test_can_be_explicitly_enabled(self) -> None:
        with patch.dict(os.environ, {"QUANT_RETENTION_MAINTENANCE_AUTOMATION_ENABLED": "true"}, clear=False):
            self.assertTrue(main.retention_maintenance_automation_enabled())


class RuntimeTaskRegistryEntryTests(unittest.TestCase):
    def test_retention_maintenance_is_declared_and_owned_by_research(self) -> None:
        contract = RUNTIME_TASK_CONTRACTS["retention_maintenance"]
        self.assertEqual(contract.owner_profile, "research")
        self.assertEqual(contract.evidence_datasets, ("retention_policies",))


class EnabledRetentionPolicyTablesTests(unittest.TestCase):
    def test_reads_only_enabled_policy_table_names(self) -> None:
        def execute(sql, _params):
            self.assertIn("WHERE enabled", sql)
            return _Result(rows=[{"table_name": "intraday_quote_observations"}, {"table_name": "market_bars_minute"}])

        tables = main._enabled_retention_policy_tables(_FakeDatabase(execute))
        self.assertEqual(tables, ["intraday_quote_observations", "market_bars_minute"])


class ApplyOneRetentionBatchTests(unittest.TestCase):
    def test_returns_the_deleted_row_count(self) -> None:
        def execute(sql, params):
            self.assertIn("quant.apply_retention_policy(%s)", sql)
            self.assertEqual(params, ("market_bars_minute",))
            return _Result({"deleted_rows": 12345})

        deleted = main._apply_one_retention_batch(_FakeDatabase(execute), "market_bars_minute")
        self.assertEqual(deleted, 12345)

    def test_returns_zero_when_no_row_is_returned(self) -> None:
        def execute(_sql, _params):
            return _Result(None)

        deleted = main._apply_one_retention_batch(_FakeDatabase(execute), "market_bars_minute")
        self.assertEqual(deleted, 0)


class RunRetentionMaintenanceOperationTests(unittest.TestCase):
    def test_loops_batches_until_a_batch_deletes_zero_rows(self) -> None:
        batch_sizes = iter([20000, 20000, 5000, 0])
        calls: list[str] = []

        def execute(sql, params):
            if sql.startswith("SELECT table_name"):
                return _Result(rows=[{"table_name": "market_bars_minute"}])
            calls.append(params[0])
            return _Result({"deleted_rows": next(batch_sizes, 0)})

        with patch.object(main, "db", _FakeDatabase(execute)):
            result = main._run_retention_maintenance_operation()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["enabled_tables"], 1)
        table_result = result["tables"]["market_bars_minute"]
        self.assertEqual(table_result["status"], "completed")
        self.assertEqual(table_result["deleted_rows"], 45000)
        self.assertEqual(table_result["batches"], 4)
        self.assertEqual(calls, ["market_bars_minute"] * 4)

    def test_a_failing_table_does_not_block_the_others(self) -> None:
        def execute(sql, params):
            if sql.startswith("SELECT table_name"):
                return _Result(rows=[{"table_name": "broken_table"}, {"table_name": "healthy_table"}])
            if params[0] == "broken_table":
                raise RuntimeError("boom")
            return _Result({"deleted_rows": 0})

        with patch.object(main, "db", _FakeDatabase(execute)):
            result = main._run_retention_maintenance_operation()

        self.assertEqual(result["tables"]["broken_table"]["status"], "failed")
        self.assertIn("boom", result["tables"]["broken_table"]["error"])
        self.assertEqual(result["tables"]["healthy_table"]["status"], "completed")

    def test_no_enabled_policies_returns_an_empty_but_completed_result(self) -> None:
        def execute(sql, _params):
            return _Result(rows=[])

        with patch.object(main, "db", _FakeDatabase(execute)):
            result = main._run_retention_maintenance_operation()

        self.assertEqual(result, {"status": "completed", "tables": {}, "enabled_tables": 0})


class RunRetentionMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_through_the_batch_lane_with_a_deduplicated_run_key(self) -> None:
        calls: list[tuple] = []

        async def fake_run_database_blocking(action, *args, **kwargs):
            calls.append((args, kwargs))
            return action()

        with patch("app.main.run_database_blocking", new=fake_run_database_blocking), \
             patch("app.main.run_recorded", return_value={"status": "completed"}) as run_recorded_mock, \
             patch("app.main.cn_today", return_value=__import__("datetime").date(2026, 9, 2)):
            result = await main.run_retention_maintenance()

        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(len(calls), 1)
        _args, kwargs = calls[0]
        self.assertEqual(kwargs["timeout_seconds"], 280)
        self.assertEqual(kwargs["lane"], "batch")
        run_recorded_mock.assert_called_once()
        self.assertEqual(run_recorded_mock.call_args.kwargs["run_key"], "retention-maintenance:2026-09-02")
        self.assertEqual(run_recorded_mock.call_args.kwargs["task_key"], "retention_maintenance")


class StartApplicationBackgroundTasksIncludesRetentionSpecTests(unittest.TestCase):
    def test_retention_maintenance_spec_is_present_and_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {
            "QUANT_BACKGROUND_TASKS_ENABLED": "true",
            "QUANT_RUNTIME_PROFILE": "full",
        }, clear=False):
            os.environ.pop("QUANT_RETENTION_MAINTENANCE_AUTOMATION_ENABLED", None)
            with patch("app.main.start_leased_background_tasks", side_effect=lambda specs, _runner: specs) as start_mock, \
                 patch("app.main.build_leased_task_runner", return_value=lambda *_a, **_k: None):
                specs = main._start_application_background_tasks()

        labels = {spec.label: spec for spec in specs}
        self.assertIn("retention_maintenance", labels)
        self.assertFalse(labels["retention_maintenance"].enabled)
        start_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import date

from app.agent_context import CONTEXT_VERSION, repository_agent_context
from app.automation_run_repository import finish_run, latest_runs, run_recorded, start_run


class Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "RETURNING run_id" in sql:
            return Result({"run_id": "run-1"})
        if "SELECT run_id,task_key" in sql:
            return Result(rows=[{"run_id": "run-1", "task_key": "analyst_market_review"}])
        return Result()


class Database:
    def __init__(self):
        self.connection = Connection()

    def transaction(self):
        class Context:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self.connection
            def __exit__(self, *_): return False
        return Context(self.connection)


class AutomationRunRepositoryTests(unittest.TestCase):
    def test_context_is_secret_free_and_stable(self):
        context = repository_agent_context()
        self.assertEqual(context["context_version"], CONTEXT_VERSION)
        self.assertNotIn("QUANT_WRITE_API_KEY", str(context))
        self.assertEqual(context["service_boundary"], "research_only_no_orders")
        self.assertIn("remote_archive_sync", context["module_map"])
        self.assertEqual(context["contracts"]["generated_frontend_types"], "frontend/src/api/generated.ts")
        self.assertEqual(context["operational_reads"]["automation_runs"], "/api/v1/automation/runs?task_key=...")
        self.assertGreaterEqual(len(context["maintenance_sequence"]), 4)

    def test_run_receipt_upsert_and_finish(self):
        connection = Connection()
        run_id = start_run(connection, task_key="analyst_market_review", run_key="review:daily:2026-08-21",
                           cadence="daily", as_of_date=date(2026, 8, 21), input_summary={"x": 1})
        finish_run(connection, run_id, output_summary={"status": "ready"})
        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(latest_runs(connection, task_key="analyst_market_review")), 1)
        self.assertGreaterEqual(len(connection.calls), 3)

    def test_recorded_operation_failure_is_rethrown(self):
        database = Database()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            run_recorded(database, task_key="test", run_key="test:failure", operation=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertTrue(any("status='failed'" in sql for sql, _ in database.connection.calls))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import date

from app.agent_context import CONTEXT_VERSION, repository_agent_context
from app.automation_run_repository import finish_run, latest_runs, run_recorded, start_or_resume_run, start_run


class Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, resumed_status: str = "running", still_running_elsewhere: bool = False):
        self.calls = []
        self._resumed_status = resumed_status
        self._still_running_elsewhere = still_running_elsewhere

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "SELECT 1 FROM quant.automation_runs" in sql:
            return Result({"exists": True} if self._still_running_elsewhere else None)
        if "RETURNING run_id,status,output_summary" in sql:
            return Result({
                "run_id": "run-1", "status": self._resumed_status,
                "output_summary": {"status": "completed"} if self._resumed_status == "completed" else {},
            })
        if "RETURNING run_id" in sql:
            return Result({"run_id": "run-1"})
        if "SELECT run_id,task_key" in sql:
            return Result(rows=[{"run_id": "run-1", "task_key": "analyst_market_review"}])
        return Result()


class Database:
    def __init__(self, connection: Connection | None = None):
        self.connection = connection or Connection()

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

    def test_recorded_operation_result_is_returned_on_success(self):
        database = Database()
        result = run_recorded(database, task_key="test", run_key="test:success", operation=lambda: {"status": "ok"})
        self.assertEqual(result, {"status": "ok"})

    def test_completed_run_key_returns_receipt_without_running_the_operation(self):
        database = Database(Connection(resumed_status="completed"))
        calls: list[str] = []
        result = run_recorded(
            database, task_key="test", run_key="test:already-done",
            operation=lambda: calls.append("ran") or {"status": "ok"},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, [])

    def test_run_key_still_running_elsewhere_is_skipped_without_running_the_operation(self):
        database = Database(Connection(still_running_elsewhere=True))
        calls: list[str] = []
        result = run_recorded(
            database, task_key="test", run_key="test:in-progress",
            operation=lambda: calls.append("ran") or {"status": "ok"},
        )
        self.assertEqual(result["status"], "skipped_running_elsewhere")
        self.assertEqual(calls, [])

    def test_process_local_in_flight_set_skips_a_concurrent_call_for_the_same_run_key(self):
        database = Database()
        in_flight: set[str] = {"test:concurrent"}
        calls: list[str] = []
        result = run_recorded(
            database, task_key="test", run_key="test:concurrent",
            operation=lambda: calls.append("ran") or {"status": "ok"},
            in_flight_run_keys=in_flight,
        )
        self.assertEqual(result["status"], "skipped_in_flight_process")
        self.assertEqual(calls, [])

    def test_in_flight_set_is_cleared_after_a_successful_run_so_the_next_call_can_proceed(self):
        database = Database()
        in_flight: set[str] = set()
        calls: list[str] = []
        run_recorded(
            database, task_key="test", run_key="test:sequential",
            operation=lambda: calls.append("first") or {"status": "ok"},
            in_flight_run_keys=in_flight,
        )
        self.assertEqual(in_flight, set())
        run_recorded(
            database, task_key="test", run_key="test:sequential",
            operation=lambda: calls.append("second") or {"status": "ok"},
            in_flight_run_keys=in_flight,
        )
        self.assertEqual(calls, ["first", "second"])

    def test_in_flight_set_is_cleared_even_after_a_failure(self):
        database = Database()
        in_flight: set[str] = set()
        with self.assertRaises(RuntimeError):
            run_recorded(
                database, task_key="test", run_key="test:fails",
                operation=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                in_flight_run_keys=in_flight,
            )
        self.assertEqual(in_flight, set())

    def test_completed_receipt_is_preserved_for_restart_resume(self):
        connection = Connection(resumed_status="completed")
        receipt = start_or_resume_run(
            connection, task_key="post_close_refresh.stage", run_key="post-close-refresh:daily:2026-08-21",
            cadence="daily", as_of_date=date(2026, 8, 21), input_summary={"stage": "daily"},
        )
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["output_summary"], {"status": "completed"})
        sql = connection.calls[0][0]
        self.assertIn("CASE WHEN quant.automation_runs.status='completed' THEN 'completed' ELSE 'running' END", sql)


if __name__ == "__main__":
    unittest.main()

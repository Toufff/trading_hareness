"""Guard the event loop from accidental direct synchronous DB transactions."""

from __future__ import annotations

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

from app.runtime_executors import BlockingExecutorBoundary, ExecutorSaturatedError
from app.async_strategy_read_repository import latest_strategy_decision
from app.async_strategy_health_repository import latest_strategy_health
from app.routers.intraday_status import build_intraday_status_router


class _DirectAsyncDbTransactionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._async_stack: list[str] = []
        self._bounded_call_depth = 0
        self.hits: list[tuple[str, int, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._async_stack.append(node.name)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A synchronous closure inside an async service is allowed only when the
        # caller submits it to the bounded database executor; it is not an
        # event-loop transaction itself.
        if self._async_stack:
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._async_stack and isinstance(node.func, ast.Attribute) and node.func.attr == "transaction":
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in {"db", "database"}:
                self.hits.append((self._async_stack[-1], node.lineno, ast.unparse(node.func)))
        is_bounded_database_call = isinstance(node.func, ast.Name) and node.func.id == "run_database_blocking"
        if is_bounded_database_call:
            self._bounded_call_depth += 1
        self.generic_visit(node)
        if is_bounded_database_call:
            self._bounded_call_depth -= 1


class _DirectAsyncRepositoryCallVisitor(ast.NodeVisitor):
    """Prevent known synchronous repository entrypoints from blocking loops."""

    _SYNC_REPOSITORY_CALLS = {
        "build_snapshot", "generate_recommendations", "recompute_outcomes",
        "recompute_scorecards", "recompute_intraday_signal_outcomes", "run_post_close_strategy",
        "resolve_sync_symbols", "ensure_catalog_capabilities", "watchlist_daily_factors", "stock_window_readiness",
        "strategy_event_context", "strategy_tushare_lhb_context", "strategy_source_readiness",
        "persist_daily_bar_batch",
    }

    def __init__(self) -> None:
        self._async_stack: list[str] = []
        self._bounded_call_depth = 0
        self.hits: list[tuple[str, int, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._async_stack.append(node.name)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._async_stack:
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        is_bounded_database_call = isinstance(node.func, ast.Name) and node.func.id == "run_database_blocking"
        if is_bounded_database_call:
            self._bounded_call_depth += 1
        if (self._async_stack and not self._bounded_call_depth and isinstance(node.func, ast.Name)
                and node.func.id in self._SYNC_REPOSITORY_CALLS):
            self.hits.append((self._async_stack[-1], node.lineno, node.func.id))
        self.generic_visit(node)
        if is_bounded_database_call:
            self._bounded_call_depth -= 1


class AsyncDatabaseBoundaryTests(unittest.TestCase):
    def test_async_functions_do_not_open_sync_database_transactions_directly(self) -> None:
        app_root = Path(__file__).resolve().parents[1] / "app"
        hits: list[str] = []
        for path in sorted(app_root.rglob("*.py")):
            visitor = _DirectAsyncDbTransactionVisitor()
            visitor.visit(ast.parse(path.read_text()))
            hits.extend(f"{path.relative_to(app_root)}:{line}:{function}:{call}" for function, line, call in visitor.hits)
        self.assertEqual(hits, [], "async DB transactions must use run_database_blocking: " + ", ".join(hits))

    def test_async_functions_offload_known_synchronous_repository_operations(self) -> None:
        app_root = Path(__file__).resolve().parents[1] / "app"
        hits: list[str] = []
        for path in sorted(app_root.rglob("*.py")):
            visitor = _DirectAsyncRepositoryCallVisitor()
            visitor.visit(ast.parse(path.read_text()))
            hits.extend(f"{path.relative_to(app_root)}:{line}:{function}:{call}" for function, line, call in visitor.hits)
        self.assertEqual(hits, [], "async repository work must use run_database_blocking: " + ", ".join(hits))


class MainRouterBoundaryTests(unittest.TestCase):
    def test_main_keeps_only_operational_control_routes(self) -> None:
        """Prevent business endpoints from drifting back into the monolith.

        All provider, research, market, intraday, sector and strategy HTTP
        contracts belong to ``app/routers``.  The three allowed direct routes
        are intentionally operational: health, metrics and an opt-in legacy
        bootstrap guard.
        """
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main_path.read_text())
        direct_routes: set[tuple[str, str]] = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                target, method = decorator.func.value, decorator.func.attr
                if not isinstance(target, ast.Name) or target.id != "app" or method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                    direct_routes.add((method.upper(), decorator.args[0].value))
        self.assertEqual(direct_routes, {
            ("GET", "/health"),
            ("GET", "/metrics"),
            ("POST", "/api/v1/bootstrap"),
        })


class BlockingExecutorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_keeps_the_slot_until_the_thread_has_really_finished(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-boundary")
        boundary = BlockingExecutorBoundary("test_timeout_boundary", workers=1, queue_capacity=0)
        started, release = threading.Event(), threading.Event()

        def slow() -> str:
            started.set()
            release.wait(1)
            return "finished"

        try:
            with self.assertRaises(asyncio.TimeoutError):
                await boundary.run(executor, slow, timeout_seconds=0.01)
            self.assertTrue(started.is_set())
            self.assertEqual(boundary.status()["occupied"], 1)
            with self.assertRaises(ExecutorSaturatedError):
                await boundary.run(executor, lambda: "should not queue", timeout_seconds=0.1)
            release.set()
            for _ in range(50):
                if boundary.status()["occupied"] == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(boundary.status()["occupied"], 0)
            self.assertEqual(await boundary.run(executor, lambda: "recovered", timeout_seconds=0.2), "recovered")
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)

    async def test_queue_capacity_is_explicitly_bounded(self) -> None:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-queue")
        boundary = BlockingExecutorBoundary("test_queue_boundary", workers=1, queue_capacity=1)
        started, release = threading.Event(), threading.Event()

        def slow() -> str:
            started.set()
            release.wait(1)
            return "finished"

        try:
            first = asyncio.create_task(boundary.run(executor, slow, timeout_seconds=1))
            while not started.is_set():
                await asyncio.sleep(0.001)
            second = asyncio.create_task(boundary.run(executor, lambda: "queued", timeout_seconds=1))
            for _ in range(50):
                if boundary.status()["occupied"] == 2:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(boundary.status()["occupied"], 2)
            with self.assertRaises(ExecutorSaturatedError):
                await boundary.run(executor, lambda: "rejected", timeout_seconds=0.1)
            release.set()
            self.assertEqual(await first, "finished")
            self.assertEqual(await second, "queued")
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)


class AsyncStrategyRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_strategy_projection_uses_native_async_execute_and_fetch(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row = row
                self.rows = rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class CursorConnection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "recommendation_runs" in sql:
                    return Result({"run_id": "run-1", "model_version": "model"})
                return Result(rows=[{"symbol": "600000.SH", "rank": 1}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = CursorConnection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        payload = await latest_strategy_decision(database, "model")
        self.assertEqual(payload["run"]["run_id"], "run-1")
        self.assertEqual(payload["recommendations"][0]["symbol"], "600000.SH")
        self.assertEqual(len(database.connection.calls), 2)
        self.assertIn("recommendations", database.connection.calls[1][0])

    async def test_intraday_status_router_prefers_async_projection_when_configured(self) -> None:
        calls = []

        async def async_status():
            calls.append("async")
            return {"summary": {"states": {"standby": 1}}}

        router = build_intraday_status_router(lambda: {"summary": {"states": {"ready": 1}}}, async_status)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/intraday/services/status")
        payload = await endpoint()
        self.assertEqual(payload["summary"]["states"], {"standby": 1})
        self.assertEqual(calls, ["async"])

    async def test_strategy_health_projection_reads_all_local_rows_async(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []
            async def fetchone(self):
                return self.row
            async def fetchall(self):
                return self.rows

        class Connection:
            async def execute(self, sql, _params=()):
                if "signal_key AS strategy_key" in sql:
                    return Result(rows=[])
                if "avg(raw_return)" in sql:
                    return Result({"rows": 0, "positive": 0, "avg_return": None})
                if "latest_quote_at" in sql:
                    return Result({"latest_quote_at": None, "fresh_quote_rows": 0})
                return Result({"signals_7d": 0, "signals_prior_7d": 0, "episodes_7d": 0,
                                "matured_30m_7d": 0, "matured_days_7d": 0})

        class Tx:
            async def __aenter__(self):
                return Connection()
            async def __aexit__(self, *_args):
                return False

        class Database:
            def transaction(self):
                return Tx()

        payload = await latest_strategy_health(Database())
        self.assertEqual(payload["status"], "research_only")
        self.assertEqual(payload["validation_gate"]["live_effect"], "none")

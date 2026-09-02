"""Guard the event loop from accidental direct synchronous DB transactions."""

from __future__ import annotations

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import patch

from app.runtime_executors import BlockingExecutorBoundary, ExecutorSaturatedError, run_akshare_blocking
from app.routers.analyst_research_reads import build_analyst_research_reads_router


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
    def test_async_read_repositories_use_native_database_parameter_names(self) -> None:
        app_root = Path(__file__).resolve().parents[1] / "app"
        for module_name in (
            "async_intraday_outcome_read_repository.py",
            "async_intraday_evidence_read_repository.py",
            "async_intraday_scan_preflight_repository.py",
            "async_intraday_scan_inputs_repository.py",
            "async_ths_concept_member_backfill_repository.py",
            "async_sync_symbol_repository.py",
            "async_runtime_lease_repository.py",
            "async_intraday_alert_outbox_repository.py",
            "async_limit_linkage_relation_repository.py",
            "async_board_rotation_outbox_repository.py",
            "async_market_result_read_repository.py",
            "async_research_catalog_read_repository.py",
            "async_research_readiness_repository.py",
            "async_analyst_skill_read_repository.py",
            "async_analyst_research_read_repository.py",
            "async_analyst_archive_read_repository.py",
            "async_board_curve_read_repository.py",
            "async_board_research_read_repository.py",
            "async_analyst_action_read_repository.py",
            "async_automation_run_read_repository.py",
            "async_market_flow_read_repository.py",
            "async_sector_read_repository.py",
            "async_limit_linkage_mining_read_repository.py",
            "async_analyst_prompt_lab_read_repository.py",
            "async_analyst_market_review_read_repository.py",
            "async_analyst_market_evaluation_read_repository.py",
            "async_analyst_stock_timeline_read_repository.py",
            "async_analyst_research_status_read_repository.py",
            "async_analyst_sync_health_repository.py",
            "async_provider_status_read_repository.py",
            "async_provider_circuit_repository.py",
            "async_market_session_repository.py",
            "async_analyst_text_feature_read_repository.py",
        ):
            tree = ast.parse((app_root / module_name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    self.assertNotIn("db", [argument.arg for argument in node.args.args], module_name)

    def test_async_functions_do_not_open_sync_database_transactions_directly(self) -> None:
        app_root = Path(__file__).resolve().parents[1] / "app"
        hits: list[str] = []
        for path in sorted(app_root.rglob("*.py")):
            visitor = _DirectAsyncDbTransactionVisitor()
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            hits.extend(f"{path.relative_to(app_root)}:{line}:{function}:{call}" for function, line, call in visitor.hits)
        self.assertEqual(hits, [], "async DB transactions must use run_database_blocking: " + ", ".join(hits))

    def test_async_functions_offload_known_synchronous_repository_operations(self) -> None:
        app_root = Path(__file__).resolve().parents[1] / "app"
        hits: list[str] = []
        for path in sorted(app_root.rglob("*.py")):
            visitor = _DirectAsyncRepositoryCallVisitor()
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            hits.extend(f"{path.relative_to(app_root)}:{line}:{function}:{call}" for function, line, call in visitor.hits)
        self.assertEqual(hits, [], "async repository work must use run_database_blocking: " + ", ".join(hits))


class MainRouterBoundaryTests(unittest.TestCase):
    def test_main_keeps_only_operational_control_routes(self) -> None:
        """Prevent business endpoints from drifting back into the monolith.

        All HTTP contracts, including health, metrics and the opt-in legacy
        bootstrap guard, belong to ``app/routers``.  The composition root only
        injects local runtime dependencies.
        """
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
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
        self.assertEqual(direct_routes, set())

    def test_legacy_sync_aliases_have_been_removed(self) -> None:
        """WP9b deleted every unused ``*_legacy`` alias; keep them from coming back."""
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        legacy_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("_legacy")
        }
        self.assertEqual(legacy_names, set())


class RouterReadBoundaryTests(unittest.TestCase):
    """Keep local dashboard reads off accidental synchronous DB routes.

    The exceptions are deliberately narrow: two static payloads and the
    compatibility branch of the injected intraday status router.  The analyst
    sync-health projection still joins n8n's public audit schema, but its
    async route now uses the bounded database executor.
    """

    _SYNC_GET_EXCEPTIONS = {
        ("automation_reads.py", "agent_context"),
        ("intraday_status.py", "intraday_services_status"),
        ("research_readiness.py", "training_roadmap"),
        # Prometheus must remain scrapeable through a plain synchronous
        # response even while the database-backed /health probe is degraded.
        ("system_control.py", "prometheus_metrics"),
    }

    def test_router_gets_are_async_or_explicit_operational_exceptions(self) -> None:
        routers = Path(__file__).resolve().parents[1] / "app" / "routers"
        sync_gets: set[tuple[str, str]] = set()
        for path in sorted(routers.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "get"
                    for decorator in node.decorator_list
                ):
                    sync_gets.add((path.name, node.name))
        self.assertEqual(sync_gets, self._SYNC_GET_EXCEPTIONS)

    def test_sync_health_uses_bounded_database_executor(self) -> None:
        calls: list[tuple[object, dict[str, object]]] = []

        async def bounded(action, *args, **kwargs):
            calls.append((action, kwargs))
            return {"runtime_verification": "bounded"}

        with patch("app.routers.analyst_research_reads.run_database_blocking", new=bounded):
            router = build_analyst_research_reads_router(object(), lambda _database, _as_of: {})
            endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")
            payload = asyncio.run(endpoint())

        self.assertEqual(payload["runtime_verification"], "bounded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].__name__, "_sync_health_payload")
        self.assertEqual(calls[0][1], {"timeout_seconds": 30})

    def test_sync_health_prefers_native_async_repository_when_available(self) -> None:
        async def native(database):
            self.assertEqual(database, "async-db")
            return {"runtime_verification": "native"}

        router = build_analyst_research_reads_router(
            object(), lambda _database, _as_of: {}, async_database="async-db", async_sync_health_fn=native,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")

        payload = asyncio.run(endpoint())

        self.assertEqual(payload["runtime_verification"], "native")


class BlockingExecutorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_source_boundary_forwards_action_keywords(self) -> None:
        result = await run_akshare_blocking(
            lambda value, *, converter: converter(value),
            "000001.SZ",
            converter=lambda value: value.replace(".", "_"),
            timeout_seconds=1,
        )
        self.assertEqual(result, "000001_SZ")

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

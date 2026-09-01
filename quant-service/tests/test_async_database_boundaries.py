"""Guard the event loop from accidental direct synchronous DB transactions."""

from __future__ import annotations

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from app.runtime_executors import BlockingExecutorBoundary, ExecutorSaturatedError, run_akshare_blocking
from app.async_strategy_read_repository import latest_strategy_decision
from app.async_strategy_health_repository import latest_strategy_health
from app.async_research_catalog_read_repository import factor_registry as async_factor_registry
from app.async_market_result_read_repository import market_snapshots as async_market_snapshots
from app.async_intraday_outcome_read_repository import latest_intraday_outcomes as async_latest_intraday_outcomes
from app.async_intraday_evidence_read_repository import latest_scan as async_latest_intraday_scan
from app.async_intraday_evidence_read_repository import watchlists as async_watchlists
from app.async_analyst_skill_read_repository import profiles as async_analyst_skill_profiles
from app.async_analyst_research_read_repository import observations as async_analyst_observations
from app.async_analyst_research_read_repository import profiles as async_analyst_research_profiles
from app.async_analyst_archive_read_repository import remote_messages as async_remote_messages
from app.async_analyst_archive_read_repository import remote_reports as async_remote_reports
from app.async_board_curve_read_repository import intraday_board_flow_curves as async_board_flow_curves
from app.async_board_curve_read_repository import latest_close_sector_review_report as async_latest_board_review
from app.async_board_research_read_repository import latest_board_rotation_events as async_board_rotations
from app.async_board_research_read_repository import latest_board_stock_mining as async_board_stock_mining
from app.async_analyst_action_read_repository import anqiang_trade_action_outcomes as async_action_outcomes
from app.async_analyst_action_read_repository import anqiang_trade_action_replay as async_action_replay
from app.async_automation_run_read_repository import latest_runs as async_automation_runs
from app.async_market_flow_read_repository import market_flow_features as async_market_flow_features
from app.async_sector_read_repository import concept_sector_signals as async_concept_signals
from app.async_sector_read_repository import market_sectors as async_market_sectors
from app.async_sector_read_repository import sector_members as async_sector_members
from app.sector_read_model import project_concept_member_backfill_status
from app.async_limit_linkage_mining_read_repository import latest_limit_linkage_mining as async_limit_linkage_mining
from app.async_analyst_prompt_lab_read_repository import status as async_prompt_lab_status
from app.async_analyst_market_review_read_repository import list_reviews as async_market_reviews
from app.async_analyst_market_evaluation_read_repository import market_evaluation as async_market_evaluation
from app.async_analyst_stock_timeline_read_repository import stock_timeline as async_stock_timeline
from app.async_analyst_research_status_read_repository import status as async_research_status
from app.async_analyst_sync_health_repository import sync_health as async_analyst_sync_health
from app.async_analyst_archive_read_repository import analyst_sync_cursor as async_archive_sync_cursor
from app.async_analyst_archive_read_repository import remote_report_list_state as async_archive_state
from app.async_provider_status_read_repository import provider_health as async_provider_health
from app.async_analyst_text_feature_read_repository import analyst_text_factor_summary as async_analyst_factor_summary
from app.async_research_readiness_repository import replay_readiness as async_replay_readiness
from app.async_research_readiness_repository import historical_estimate as async_historical_estimate
from app.request_models import HistoricalCoverageEstimateRequest
from app.routers.intraday_status import build_intraday_status_router
from app.routers.event_reads import build_event_reads_router
from app.routers.research_readiness import build_research_readiness_router
from app.routers.analyst_skill_reads import build_analyst_skill_reads_router
from app.routers.analyst_research_reads import build_analyst_research_reads_router
from app.routers.analyst_reads import build_analyst_reads_router
from app.routers.board_curve_reads import build_board_curve_reads_router
from app.routers.board_rotation_reads import build_board_rotation_reads_router
from app.routers.board_stock_mining_reads import build_board_stock_mining_reads_router
from app.routers.analyst_trade_action_reads import build_analyst_trade_action_reads_router
from app.routers.analyst_action_outcomes import build_analyst_action_outcomes_router
from app.routers.automation_reads import build_automation_reads_router
from app.routers.market_flow_reads import build_market_flow_reads_router
from app.routers.sector_reads import build_sector_reads_router
from app.routers.limit_linkage_mining_reads import build_limit_linkage_mining_reads_router
from app.routers.analyst_prompt_lab import build_analyst_prompt_lab_router
from app.routers.provider_status import build_provider_status_router


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

    def test_legacy_sync_names_are_thin_compatibility_aliases(self) -> None:
        """Prevent removed provider implementations from returning to main.py."""
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        names = {
            "sync_tushare_legacy", "sync_baostock_legacy", "sync_market_universe_legacy",
            "sync_full_market_daily_legacy", "sync_ths_sector_catalog_legacy",
            "sync_eastmoney_board_members_legacy", "sync_ths_industry_moneyflow_legacy",
            "sync_ths_concept_signals_legacy", "sync_ths_concept_members_legacy",
            "review_claim_legacy",
        }
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                found[node.name] = node
        self.assertEqual(set(found), names)
        for name, node in found.items():
            self.assertLessEqual(len(node.body), 4, name)


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
        # Health probes the strict synchronous local database control plane;
        # Prometheus must remain scrapeable even while that probe is degraded.
        ("system_control.py", "health"),
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

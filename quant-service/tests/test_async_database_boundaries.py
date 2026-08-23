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
            tree = ast.parse((app_root / module_name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    self.assertNotIn("db", [argument.arg for argument in node.args.args], module_name)

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

    def test_legacy_sync_names_are_thin_compatibility_aliases(self) -> None:
        """Prevent removed provider implementations from returning to main.py."""
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main_path.read_text())
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
    }

    def test_router_gets_are_async_or_explicit_operational_exceptions(self) -> None:
        routers = Path(__file__).resolve().parents[1] / "app" / "routers"
        sync_gets: set[tuple[str, str]] = set()
        for path in sorted(routers.glob("*.py")):
            tree = ast.parse(path.read_text())
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


class AsyncStrategyRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_intraday_evidence_lists_use_native_async_connection(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "intraday_watchlists" in sql:
                    return Result(rows=[{"symbol": "600176.SH"}])
                if "intraday_scan_runs" in sql:
                    return Result(row={"scan_id": "scan-1"})
                if "intraday_signal_events WHERE" in sql:
                    return Result(rows=[{"signal_event_id": "signal-1"}])
                return Result(rows=[{"delivery_id": "delivery-1"}])

        class Transaction:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Transaction(self.connection)

        database = Database()
        self.assertEqual((await async_watchlists(database))["items"][0]["symbol"], "600176.SH")
        payload = await async_latest_intraday_scan(database, limit=10_000)
        self.assertEqual(payload["scan"]["scan_id"], "scan-1")
        self.assertEqual(payload["signals"][0]["signal_event_id"], "signal-1")
        self.assertEqual(payload["deliveries"][0]["delivery_id"], "delivery-1")
        self.assertEqual(database.connection.calls[-2][1], ("scan-1", 200))
        self.assertEqual(database.connection.calls[-1][1], ("scan-1", 200))

    async def test_intraday_outcome_projection_uses_native_async_connection(self) -> None:
        observed_at = __import__("datetime").datetime(2026, 8, 14, 2, 0, tzinfo=__import__("datetime").timezone.utc)

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, _params=()):
                self.calls.append(sql)
                if "JOIN quant.intraday_signal_events" in sql:
                    return Result([{
                        "signal_event_id": "event-1", "symbol": "000001.SZ", "signal_key": "entry-v1",
                        "signal_type": "entry", "observed_at": observed_at, "conditions": {}, "evidence": {},
                    }])
                if "FROM quant.intraday_board_reports" in sql:
                    return Result([])
                return Result([{"horizon_key": "30m", "status": "matured", "rows": 1}])

        class Transaction:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Transaction(self.connection)

        database = Database()
        payload = await async_latest_intraday_outcomes(
            database, 1000,
            market_context_from_board_report_fn=lambda *_args: {"status": "available"},
            attribution_fn=lambda *_args: {"stage": "generic"},
            attribution_summary_fn=lambda _rows: {"items": [], "validation_gate": {"status": "accumulating"}},
        )
        self.assertEqual(payload["items"][0]["attribution"]["stage"], "generic")
        self.assertEqual(payload["summary"][0]["rows"], 1)
        self.assertEqual(len(database.connection.calls), 3)

    async def test_catalog_and_market_result_projections_use_native_async_connection(self) -> None:
        class Result:
            def __init__(self, rows=None):
                self.rows = rows or []
            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []
            async def execute(self, sql, _params=()):
                self.calls.append(sql)
                return Result([{"factor_key": "momentum"}] if "factor_registry" in sql else [{"status": "completed"}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        db = Database()
        self.assertEqual((await async_factor_registry(db))["items"][0]["factor_key"], "momentum")
        self.assertEqual((await async_market_snapshots(db, 10))["items"][0]["status"], "completed")
        self.assertEqual(len(db.connection.calls), 2)

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

    async def test_analyst_skill_router_prefers_async_persisted_projection(self) -> None:
        calls = []

        async def async_profiles(_database, analyst_id, limit):
            calls.append((analyst_id, limit))
            return {"items": [{"remote_analyst_id": "anqiang"}], "model_version": "test"}

        router = build_analyst_skill_reads_router(
            object(), lambda *_args: {"items": []}, async_database=object(), async_profiles_fn=async_profiles,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-skills")
        payload = await endpoint("anqiang", 7)
        self.assertEqual(payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(calls, [("anqiang", 7)])

    async def test_analyst_skill_projection_uses_native_async_connection(self) -> None:
        class Result:
            async def fetchall(self):
                return [{"remote_analyst_id": "anqiang", "profile": {}}]

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result()

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        payload = await async_analyst_skill_profiles(database, "anqiang", 500)
        self.assertEqual(payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(database.connection.calls[0][1], ("anqiang", 100))

    async def test_analyst_research_router_prefers_async_local_evidence(self) -> None:
        calls = []

        async def async_profiles(_database):
            calls.append("profiles")
            return {"items": [{"remote_analyst_id": "anqiang"}]}

        async def async_observations(_database, analyst_id, limit):
            calls.append((analyst_id, limit))
            return {"items": [{"analyst_id": analyst_id}], "health": []}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(),
            async_profiles_fn=async_profiles, async_observations_fn=async_observations,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        profile_payload = await endpoints["/api/v1/analyst-research/profiles"]()
        observation_payload = await endpoints["/api/v1/analyst-research/observations"]("anqiang", 9)
        self.assertEqual(profile_payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(observation_payload["items"][0]["analyst_id"], "anqiang")
        self.assertEqual(calls, ["profiles", ("anqiang", 9)])

    async def test_analyst_research_projections_use_native_async_connection(self) -> None:
        class Result:
            def __init__(self, rows):
                self.rows = rows

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "remote_analysts" in sql:
                    return Result([{"remote_analyst_id": "anqiang"}])
                if "FROM quant.analyst_observations" in sql and "GROUP BY" not in sql:
                    return Result([{"analyst_id": "anqiang", "observation_id": "o-1"}])
                return Result([{"analyst_id": "anqiang", "observations": 1}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        profile_payload = await async_analyst_research_profiles(database)
        observation_payload = await async_analyst_observations(database, "anqiang", 900)
        self.assertEqual(profile_payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(observation_payload["health"][0]["observations"], 1)
        self.assertEqual(database.connection.calls[-2][1], ("anqiang", "anqiang", 500))

    async def test_analyst_archive_router_prefers_async_text_only_pages(self) -> None:
        calls = []

        async def reports(_database, limit, offset):
            calls.append(("reports", limit, offset))
            return {"items": [], "total": 0}

        async def messages(_database, analyst_id, limit, offset):
            calls.append(("messages", analyst_id, limit, offset))
            return {"items": [], "total": 0}

        async def claims(_database, limit, offset):
            calls.append(("claims", limit, offset))
            return {"items": [], "total": 0}

        async def review(_database, status, limit):
            calls.append(("review", status, limit))
            return {"items": [], "status": status}

        router = build_analyst_reads_router(
            object(), lambda *_args: {}, lambda *_args: {}, async_database=object(),
            async_remote_reports_fn=reports, async_remote_messages_fn=messages,
            async_analyst_claims_fn=claims, async_claim_review_queue_fn=review,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        await endpoints["/api/v1/remote-archive/reports"](7, 2)
        await endpoints["/api/v1/remote-archive/messages"]("anqiang", 8, 3)
        await endpoints["/api/v1/analyst-claims"](9, 4)
        await endpoints["/api/v1/claim-review"]("approved", 10)
        self.assertEqual(calls, [
            ("reports", 7, 2), ("messages", "anqiang", 8, 3),
            ("claims", 9, 4), ("review", "approved", 10),
        ])

    async def test_analyst_archive_pagination_is_bounded_in_native_async_repository(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "count(*)" in sql:
                    return Result({"total": 1})
                return Result(rows=[{"remote_report_id": "r-1"}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        reports = await async_remote_reports(database, 1000, -2)
        messages = await async_remote_messages(database, "anqiang", 1000, -3)
        self.assertEqual(reports["limit"], 100)
        self.assertEqual(reports["offset"], 0)
        self.assertEqual(messages["limit"], 100)
        self.assertEqual(messages["offset"], 0)
        self.assertEqual(database.connection.calls[0][1], (100, 0))
        self.assertEqual(database.connection.calls[2][1], ("anqiang", "anqiang", 100, 0))

    async def test_board_curve_router_prefers_async_persisted_projection(self) -> None:
        calls = []

        async def curves(_database, trade_date, taxonomy, since, **kwargs):
            calls.append((trade_date, taxonomy, since, kwargs))
            return {"items": [], "taxonomy": taxonomy}

        async def review(_database):
            calls.append("review")
            return {"report": None}

        router = build_board_curve_reads_router(
            object(), lambda: 60, lambda: 60, async_database=object(),
            async_curves_fn=curves, async_latest_review_fn=review,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        payload = await endpoints["/api/v1/market/sectors/intraday/curves"](None, "concept", None)
        review_payload = await endpoints["/api/v1/market/sectors/review/report/latest"]()
        self.assertEqual(payload["taxonomy"], "concept")
        self.assertIsNone(review_payload["report"])
        self.assertEqual(calls[0][1], "concept")
        self.assertEqual(calls[0][3], {"curve_retention_days": 60, "rotation_retention_days": 60})
        self.assertEqual(calls[1], "review")

    async def test_board_curve_repository_uses_native_async_rows_and_shared_projection(self) -> None:
        observed_at = datetime(2026, 8, 10, 1, 21, tzinfo=timezone.utc)

        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "board_reports ORDER BY" in sql:
                    return Result({"board_report_id": "report-1"})
                if "intraday_board_flow_snapshots" in sql:
                    return Result(rows=[{
                        "observed_at": observed_at, "status": "completed",
                        "coverage": {"concept": {"flow_boards": 1}},
                        "payload": {"items": [{
                            "taxonomy_key": "eastmoney_concept", "sector_key": "BK1", "label": "芯片",
                            "net_inflow": 2.5, "change_pct": 1.0,
                        }]}, "source": "minute_curve",
                    }])
                return Result(rows=[])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        review = await async_latest_board_review(database)
        curves = await async_board_flow_curves(
            database, date(2026, 8, 10), "concept", None,
            curve_retention_days=60, rotation_retention_days=60, now=observed_at,
        )
        self.assertEqual(review["report"]["board_report_id"], "report-1")
        self.assertEqual(curves["items"][0]["label"], "芯片")
        self.assertEqual(len(database.connection.calls), 3)

    async def test_board_rotation_and_mining_routers_prefer_async_evidence(self) -> None:
        calls = []

        async def rotations(_database, limit):
            calls.append(("rotations", limit))
            return {"items": [{"rotation_event_id": "event-1"}]}

        async def mining(_database, limit):
            calls.append(("mining", limit))
            return {"run": {"mining_run_id": "run-1"}, "inflow": [], "outflow": []}

        rotation_router = build_board_rotation_reads_router(object(), async_database=object(), async_events_fn=rotations)
        mining_router = build_board_stock_mining_reads_router(object(), async_database=object(), async_mining_fn=mining)
        rotation_endpoint = rotation_router.routes[0].endpoint
        mining_endpoint = mining_router.routes[0].endpoint
        self.assertEqual((await rotation_endpoint(101))["items"][0]["rotation_event_id"], "event-1")
        self.assertEqual((await mining_endpoint(51))["run"]["mining_run_id"], "run-1")
        self.assertEqual(calls, [("rotations", 101), ("mining", 51)])

    async def test_board_rotation_and_mining_repositories_bound_local_rows(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "mining_runs" in sql:
                    return Result({"mining_run_id": "run-1"})
                if "mining_candidates" in sql:
                    return Result(rows=[
                        {"direction": "inflow", "symbol": "600000.SH"},
                        {"direction": "outflow", "symbol": "000001.SZ"},
                    ])
                return Result(rows=[{"rotation_event_id": "event-1"}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        rotations = await async_board_rotations(database, 1000)
        mining = await async_board_stock_mining(database, 1000)
        self.assertEqual(rotations["items"][0]["rotation_event_id"], "event-1")
        self.assertEqual(mining["inflow"][0]["symbol"], "600000.SH")
        self.assertEqual(mining["outflow"][0]["symbol"], "000001.SZ")
        self.assertEqual(database.connection.calls[0][1], (100,))

    async def test_analyst_action_routers_prefer_async_persisted_evidence(self) -> None:
        calls = []

        async def replay(_database, as_of_date, limit):
            calls.append(("replay", as_of_date, limit))
            return {"items": [], "limit": limit}

        async def outcomes(_database):
            calls.append(("outcomes",))
            return {"outcomes": []}

        action_router = build_analyst_trade_action_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_replay_fn=replay,
        )
        outcome_router = build_analyst_action_outcomes_router(
            object(), lambda *_args, **_kwargs: {}, async_database=object(), async_outcomes_fn=outcomes,
        )
        action = await action_router.routes[0].endpoint(date(2026, 8, 10), 201)
        outcome = await outcome_router.routes[0].endpoint()
        self.assertEqual(action["limit"], 201)
        self.assertEqual(outcome["outcomes"], [])
        self.assertEqual(calls, [("replay", date(2026, 8, 10), 201), ("outcomes",)])

    async def test_analyst_action_repositories_use_native_async_local_evidence(self) -> None:
        stated_at = datetime(2026, 8, 10, 2, tzinfo=timezone.utc)

        class Result:
            def __init__(self, rows=None):
                self.rows = rows or []

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "analyst_action_intraday_outcomes" in sql:
                    return Result([{"methodology_version": "v1", "count": 1}])
                return Result([{
                    "action_id": "action-1", "stated_at": stated_at, "available_at": stated_at,
                    "quote_price": 10.0, "session_close_price": 10.2, "daily_close": 10.2,
                }])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        replay = await async_action_replay(database, date(2026, 8, 10), 1000)
        outcomes = await async_action_outcomes(database)
        self.assertEqual(replay["items"][0]["evaluation_quality"], "persisted_intraday_quote")
        self.assertTrue(replay["items"][0]["factor_eligible"])
        self.assertEqual(outcomes["outcomes"][0]["count"], 1)
        self.assertEqual(database.connection.calls[0][1], (date(2026, 8, 10), 200))

    async def test_automation_run_router_prefers_async_receipts(self) -> None:
        calls = []

        async def latest(_database, task_key, limit):
            calls.append((task_key, limit))
            return [{"task_key": task_key, "status": "completed"}]

        router = build_automation_reads_router(object(), async_database=object(), async_latest_runs_fn=latest)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/automation/runs")
        payload = await endpoint("post_close", 1000)
        self.assertEqual(payload["items"][0]["status"], "completed")
        self.assertEqual(calls, [("post_close", 100)])

    async def test_automation_run_repository_uses_native_async_bounded_query(self) -> None:
        class Result:
            async def fetchall(self):
                return [{"run_id": "run-1", "status": "completed"}]

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result()

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        rows = await async_automation_runs(database, "post_close", 1000)
        self.assertEqual(rows[0]["run_id"], "run-1")
        self.assertEqual(database.connection.calls[0][1], ("post_close", "post_close", 100))

    async def test_market_flow_router_prefers_async_persisted_projection(self) -> None:
        calls = []

        async def features(_database, trade_date, *, limit):
            calls.append((trade_date, limit))
            return {"trade_date": str(trade_date), "items": []}

        router = build_market_flow_reads_router(object(), async_database=object(), async_features_fn=features)
        endpoint = router.routes[0].endpoint
        payload = await endpoint(date(2026, 8, 10), 1001)
        self.assertEqual(payload["trade_date"], "2026-08-10")
        self.assertEqual(calls, [(date(2026, 8, 10), 1001)])

    async def test_market_flow_repository_uses_native_async_rows_and_research_gate(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "DISTINCT ON(exchange_date)" in sql:
                    return Result(rows=[])
                if "sector_flow_daily_features feature" in sql:
                    return Result(rows=[])
                if "sector_flow_daily_outcomes" in sql and "GROUP BY" in sql:
                    return Result(rows=[])
                if "matured_events" in sql:
                    return Result({"trading_days": 59, "matured_events": 199})
                return Result(rows=[{"market_state": "rotation", "feature_key": "minute-1"}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        payload = await async_market_flow_features(database, date(2026, 8, 10), limit=5000)
        self.assertEqual(payload["items"][0]["feature_key"], "minute-1")
        self.assertEqual(payload["research_gate"]["status"], "accumulating")
        self.assertEqual(database.connection.calls[0][1], (date(2026, 8, 10), 1000))

    async def test_sector_router_prefers_async_exact_evidence_projections(self) -> None:
        calls = []

        async def backfill(_database, trade_date, **kwargs):
            calls.append(("backfill", trade_date, kwargs))
            return {"states": []}

        async def concepts(_database, trade_date, limit):
            calls.append(("concepts", trade_date, limit))
            return {"items": []}

        async def candidates(_database, trade_date, limit):
            calls.append(("candidates", trade_date, limit))
            return {"items": []}

        async def flows(_database, taxonomy, trade_date, limit):
            calls.append(("flows", taxonomy, trade_date, limit))
            return {"items": []}

        async def sectors(_database, taxonomy, limit, offset):
            calls.append(("sectors", taxonomy, limit, offset))
            return {"items": []}

        async def members(_database, sector_key, taxonomy, limit, offset):
            calls.append(("members", sector_key, taxonomy, limit, offset))
            return {"items": []}

        router = build_sector_reads_router(
            object(), lambda: True, lambda: 25, async_database=object(),
            async_backfill_status_fn=backfill, async_concepts_fn=concepts, async_candidates_fn=candidates,
            async_flows_fn=flows, async_sectors_fn=sectors, async_members_fn=members,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        await endpoints["/api/v1/market/sectors/concepts/members/backfill/status"](date(2026, 8, 10))
        await endpoints["/api/v1/market/sectors/concepts"](None, 500)
        await endpoints["/api/v1/market/sectors/concepts/candidates"](None, 100)
        await endpoints["/api/v1/market/sectors/flows"]("ths_industry", None, 100)
        await endpoints["/api/v1/market/sectors"]("ths_index_n", 500, 0)
        await endpoints["/api/v1/market/sectors/{sector_key}/members"]("885001", "ths_index_n", 500, 0)
        self.assertEqual(calls[0], ("backfill", date(2026, 8, 10), {"automatic_enabled": True, "batch_size": 25}))
        self.assertEqual([call[0] for call in calls[1:]], ["concepts", "candidates", "flows", "sectors", "members"])

    async def test_sector_repositories_use_native_async_bounds_and_shared_scoring(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "percent_rank" in sql:
                    return Result(rows=[{
                        "flow_percentile": 1.0, "change_pct": 2.0, "up_nums": None,
                        "streak_days": None, "raw": {}, "strength_raw": {}, "label": "芯片",
                    }])
                if "count(m.symbol)" in sql:
                    return Result(rows=[{"sector_key": "885001", "label": "半导体"}])
                if "count(*)::int total FROM quant.sectors" in sql:
                    return Result({"total": 2})
                if "sector_membership_history m JOIN" in sql:
                    return Result(rows=[{"symbol": "600000.SH"}])
                if "effective_to IS NULL" in sql:
                    return Result({"total": 3})
                return Result({"latest": date(2026, 8, 10)})

        class Transaction:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Transaction(self.connection)

        database = Database()
        concepts = await async_concept_signals(database, date(2026, 8, 10), 10000)
        sectors = await async_market_sectors(database, "ths_index_n", 10000, -1)
        members = await async_sector_members(database, "885001", "ths_index_n", 10000, -1)
        self.assertEqual(concepts["items"][0]["aggregate_score"], 86.0)
        self.assertEqual(sectors["limit"], 1000)
        self.assertEqual(members["total"], 3)
        self.assertEqual(database.connection.calls[0][1], (date(2026, 8, 10), date(2026, 8, 10), 1000))

    def test_concept_mapping_status_separates_active_exact_coverage_from_receipts(self) -> None:
        payload = project_concept_member_backfill_status(
            date(2026, 8, 21), 387, 0,
            {"mapped_concepts": 387, "member_rows": 70998, "latest_available_at": "2026-08-21T12:40:26+00:00"}, [],
            automatic_enabled=False, batch_size=25,
        )
        self.assertTrue(payload["complete"])
        self.assertFalse(payload["receipt_complete"])
        self.assertEqual(payload["mapped_concepts"], 387)
        self.assertEqual(payload["receipt_mapped_concepts"], 0)
        self.assertEqual(payload["states"][0]["state"], "active_exact_mapping")

    async def test_limit_linkage_router_prefers_async_exact_relation_evidence(self) -> None:
        calls = []

        async def linkage(_database, limit):
            calls.append(limit)
            return {"items": [{"symbol": "600000.SH"}]}

        router = build_limit_linkage_mining_reads_router(
            object(), async_database=object(), async_linkage_fn=linkage,
        )
        payload = await router.routes[0].endpoint(51)
        self.assertEqual(payload["items"][0]["symbol"], "600000.SH")
        self.assertEqual(calls, [51])

    async def test_limit_linkage_repository_uses_native_async_bounded_rows(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "mining_runs" in sql:
                    return Result({"linkage_run_id": "run-1"})
                return Result(rows=[{"symbol": "600000.SH"}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_limit_linkage_mining(database, 1000)
        self.assertEqual(payload["items"][0]["symbol"], "600000.SH")
        self.assertEqual(database.connection.calls[1][1], ("run-1", 50))

    async def test_prompt_lab_status_router_prefers_async_research_only_projection(self) -> None:
        calls = []

        async def status(_database, limit):
            calls.append(limit)
            return {"candidates": [], "live_effect": "none"}

        router = build_analyst_prompt_lab_router(
            object(), lambda *_args, **_kwargs: {}, lambda *_args, **_kwargs: {},
            lambda *_args, **_kwargs: {}, lambda *_args, **_kwargs: {},
            async_database=object(), async_status_fn=status,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-prompt-lab/status")
        payload = await endpoint(1000)
        self.assertEqual(payload["live_effect"], "none")
        self.assertEqual(calls, [1000])

    async def test_prompt_lab_status_uses_native_async_bounded_evidence(self) -> None:
        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "prompt_candidates" in sql: return Result([{"candidate_id": "candidate-1"}])
                if "evaluation_runs" in sql: return Result([{"evaluation_id": "evaluation-1"}])
                return Result([{"methodology_version": "v1", "count": 1}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_prompt_lab_status(database, 1000)
        self.assertEqual(payload["candidates"][0]["candidate_id"], "candidate-1")
        self.assertEqual(payload["evaluations"][0]["evaluation_id"], "evaluation-1")
        self.assertEqual(database.connection.calls[0][1], (500,))

    async def test_analyst_review_reads_prefer_native_async_bounded_evidence(self) -> None:
        calls = []

        async def reviews(_database, cadence, limit):
            calls.append((cadence, limit))
            return {"items": [{"review_id": "review-1"}], "live_effect": "none"}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_list_reviews_fn=reviews,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/reviews")
        payload = await endpoint("daily", 1000)
        self.assertEqual(payload["items"][0]["review_id"], "review-1")
        self.assertEqual(calls, [("daily", 1000)])

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result([{"review_id": "review-1"}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_market_reviews(database, "daily", 1000)
        self.assertEqual(direct["items"][0]["review_id"], "review-1")
        self.assertEqual(database.connection.calls[0][1], ("daily", "daily", 100))

    async def test_analyst_market_evaluation_prefers_native_async_evidence(self) -> None:
        calls = []

        async def evaluation(_database, start, end, analyst):
            calls.append((start, end, analyst))
            return {"quality_gate": {"live_strategy_effect": "none"}}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_market_evaluation_fn=evaluation,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/market-evaluation")
        start, end = date(2026, 8, 1), date(2026, 8, 15)
        payload = await endpoint(start, end, "anqiang-touzi-riji")
        self.assertEqual(payload["quality_gate"]["live_strategy_effect"], "none")
        self.assertEqual(calls, [(start, end, "anqiang-touzi-riji")])

        class Result:
            async def fetchall(self): return []

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_market_evaluation(database, start, end, "anqiang-touzi-riji")
        self.assertEqual(direct["quality_gate"]["live_strategy_effect"], "none")
        self.assertEqual(len(database.connection.calls), 8)
        self.assertEqual(database.connection.calls[0][1], (start, end, "anqiang-touzi-riji", "anqiang-touzi-riji"))

    async def test_analyst_stock_timeline_prefers_native_async_local_evidence(self) -> None:
        calls = []

        async def timeline(_database, **kwargs):
            calls.append(kwargs)
            return {"symbol": kwargs["symbol"], "bar_count": 0, "boundary": "no media is fetched"}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_stock_timeline_fn=timeline,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/stock-timeline")
        payload = await endpoint("600000.SH", date(2026, 8, 21), date(2026, 8, 21), "anqiang-touzi-riji", 9999)
        self.assertEqual(payload["symbol"], "600000.SH")
        self.assertEqual(calls[0]["limit"], 9999)

        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "max(trading_date)" in sql: return Result({"latest_date": date(2026, 8, 21)})
                return Result(rows=[])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_stock_timeline(database, symbol="600000.SH", start_date=date(2026, 8, 21), limit=9999)
        self.assertEqual(direct["bar_count"], 0)
        self.assertEqual(database.connection.calls[1][1][-1], 3000)

    async def test_analyst_research_status_prefers_native_async_local_evidence(self) -> None:
        calls = []

        async def status(_database, as_of_date):
            calls.append(as_of_date)
            return {"approved_theme_board_aliases": 1, "boundary": "research-only"}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_status_fn=status,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/status")
        as_of = date(2026, 8, 21)
        payload = await endpoint(as_of)
        self.assertEqual(payload["approved_theme_board_aliases"], 1)
        self.assertEqual(calls, [as_of])

        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "count(*)" in sql: return Result({"count": 2})
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_research_status(database, as_of)
        self.assertEqual(direct["approved_theme_board_aliases"], 2)
        self.assertEqual(len(database.connection.calls), 5)

    async def test_analyst_archive_state_and_cursor_prefer_native_async_local_evidence(self) -> None:
        calls = []

        async def state(_database):
            calls.append("state")
            return {"analysts": [{"remote_analyst_id": "anqiang-touzi-riji"}]}

        async def cursor(_database, stream_key, analyst_id):
            calls.append((stream_key, analyst_id))
            return {"stream_key": stream_key, "remote_analyst_id": analyst_id, "cursor": {}}

        router = build_analyst_reads_router(
            object(), lambda *_args: {}, lambda *_args: {}, async_database=object(),
            async_remote_archive_state_fn=state, async_sync_cursor_fn=cursor,
        )
        state_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/remote-archive/state")
        cursor_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/remote-archive/sync-cursors/{stream_key}/{analyst_id}")
        self.assertEqual((await state_endpoint())["analysts"][0]["remote_analyst_id"], "anqiang-touzi-riji")
        self.assertEqual((await cursor_endpoint("messages", "anqiang-touzi-riji"))["stream_key"], "messages")
        self.assertEqual(calls, ["state", ("messages", "anqiang-touzi-riji")])

        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "remote_analysts a" in sql: return Result(rows=[{"remote_analyst_id": "anqiang-touzi-riji"}])
                return Result({"stream_key": "messages", "remote_analyst_id": "anqiang-touzi-riji"})

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        self.assertEqual((await async_archive_state(database))["analysts"][0]["remote_analyst_id"], "anqiang-touzi-riji")
        direct = await async_archive_sync_cursor(database, "messages", "anqiang-touzi-riji")
        self.assertEqual(direct["remote_analyst_id"], "anqiang-touzi-riji")
        self.assertEqual(database.connection.calls[-1][1], ("messages", "anqiang-touzi-riji"))

    async def test_provider_health_prefers_native_async_local_evidence(self) -> None:
        calls = []

        async def health(_database, configs, observed_at):
            calls.append((configs, observed_at))
            return {"summary": {"healthy": 1}, "items": []}

        router = build_provider_status_router(
            object(), lambda: [{"provider_key": "tushare_super_get", "configured": True}], lambda: [],
            async_database=object(), async_provider_health_fn=health,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/providers/health")
        self.assertEqual((await endpoint())["summary"]["healthy"], 1)
        self.assertEqual(calls[0][0][0]["provider_key"], "tushare_super_get")

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result([{"provider_key": "tushare_super_get", "enabled": True, "capability": "rt_k", "market": "cn",
                                "circuit_open_until": None, "last_success_at": None, "last_failure_at": None}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_provider_health(
            database, [{"provider_key": "tushare_super_get", "configured": True}], datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        self.assertEqual(direct["items"][0]["state"], "unknown")
        self.assertEqual(len(database.connection.calls), 1)

    async def test_analyst_factors_prefer_native_async_text_only_evidence(self) -> None:
        calls = []

        async def summary(_database, as_of_date, lookback_days):
            calls.append((as_of_date, lookback_days))
            return {"factor_version": "analyst-text-consensus-v1", "data_boundary": "text-only"}

        router = build_analyst_reads_router(
            object(), lambda *_args: {}, lambda *_args: {}, async_database=object(), async_factor_summary_fn=summary,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-factors")
        as_of = date(2026, 8, 21)
        self.assertEqual((await endpoint(as_of, 100)) ["data_boundary"], "text-only")
        self.assertEqual(calls, [(as_of, 100)])

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result([])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_analyst_factor_summary(database, as_of, 100)
        self.assertEqual(direct["lookback_days"], 30)
        self.assertEqual(database.connection.calls[0][1][:2], (date(2026, 7, 23), as_of))

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

    async def test_replay_readiness_projection_uses_native_async_connection(self) -> None:
        class Result:
            async def fetchone(self):
                return {
                    "full_cross_section_days": 0, "offline_minute_trading_days": 0,
                    "offline_minute_symbols": 0, "offline_minute_bars": 0,
                    "completed_offline_imports": 0, "confirmed_signal_events": 0,
                    "matured_signal_events": 0,
                }

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, _params=()):
                self.calls.append(sql)
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_replay_readiness(database)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(len(database.connection.calls), 1)
        self.assertIn("canonical_bars_daily", database.connection.calls[0])

    async def test_research_readiness_router_prefers_async_replay_projection(self) -> None:
        calls = []

        class Result:
            async def fetchone(self):
                return {"full_cross_section_days": 0, "offline_minute_trading_days": 0,
                        "offline_minute_symbols": 0, "offline_minute_bars": 0,
                        "completed_offline_imports": 0, "confirmed_signal_events": 0,
                        "matured_signal_events": 0}

        class Connection:
            async def execute(self, _sql, _params=()):
                calls.append("async")
                return Result()

        class Tx:
            async def __aenter__(self): return Connection()
            async def __aexit__(self, *_args): return False

        class Database:
            def transaction(self): return Tx()

        def must_not_run(_database):
            raise AssertionError("sync replay readiness path was selected")

        router = build_research_readiness_router(
            object(), lambda _request: {}, lambda _database: {}, must_not_run,
            async_database=Database(),
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/data-readiness/replay")
        payload = await endpoint()
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(calls, ["async"])

    async def test_historical_estimate_projection_uses_native_async_connection(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "quant.sectors" in sql: return Result({"total": 0})
                if "canonical_bars_daily" in sql: return Result({"first_bar_date": None, "latest_bar_date": None,
                    "bar_days": 0, "full_cross_section_days": 0, "max_symbols_on_day": 0,
                    "fundamental_symbols": 0, "limit_symbols": 0, "minute_symbols": 0})
                if "tushare_raw_records" in sql: return Result(rows=[])
                return Result({"symbols": 5500})

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_historical_estimate(database, HistoricalCoverageEstimateRequest())
        self.assertEqual(payload["current_coverage"]["bar_days"], 0)
        self.assertEqual(len(database.connection.calls), 4)

    async def test_event_router_prefers_async_local_projection(self) -> None:
        async def announcements(*_args, **_kwargs):
            return {"items": [], "async": True}
        async def lhb(*_args, **_kwargs):
            return {"items": [], "async": True}
        router = build_event_reads_router(None, object())
        # Route assembly uses the production async repository; this smoke
        # check also guards that both public endpoints remain GET-only.
        self.assertEqual({route.path: route.methods for route in router.routes}, {
            "/api/v1/events/announcements": {"GET"}, "/api/v1/events/lhb": {"GET"},
        })

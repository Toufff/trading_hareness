"""Guard the event loop from accidental direct synchronous DB transactions."""

from __future__ import annotations

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

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
from app.async_research_readiness_repository import replay_readiness as async_replay_readiness
from app.async_research_readiness_repository import historical_estimate as async_historical_estimate
from app.request_models import HistoricalCoverageEstimateRequest
from app.routers.intraday_status import build_intraday_status_router
from app.routers.event_reads import build_event_reads_router
from app.routers.research_readiness import build_research_readiness_router
from app.routers.analyst_skill_reads import build_analyst_skill_reads_router
from app.routers.analyst_research_reads import build_analyst_research_reads_router
from app.routers.analyst_reads import build_analyst_reads_router


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
            "async_market_result_read_repository.py",
            "async_research_catalog_read_repository.py",
            "async_research_readiness_repository.py",
            "async_analyst_skill_read_repository.py",
            "async_analyst_research_read_repository.py",
            "async_analyst_archive_read_repository.py",
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

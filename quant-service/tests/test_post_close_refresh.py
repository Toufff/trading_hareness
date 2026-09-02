from __future__ import annotations

import asyncio
import unittest
from datetime import date

from app.post_close_refresh import record_stage_with_receipt, run_refresh
from app.runtime_leases import LeaseLostError
from app.post_close_refresh_service import (
    POST_CLOSE_STAGE_DEPENDENCIES,
    POST_CLOSE_STAGE_ORDER,
    POST_CLOSE_TIMEOUT_OVERRIDES,
    PostCloseRefreshDependencies,
    run_post_close_refresh,
)
from app.request_models import PostCloseRefreshRequest


class PostCloseRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_outcome_settlement_has_explicit_long_bounded_budget(self):
        self.assertEqual(POST_CLOSE_TIMEOUT_OVERRIDES["analyst_outcomes"], 300.0)
        self.assertEqual(POST_CLOSE_TIMEOUT_OVERRIDES["analyst_intraday_outcomes"], 180.0)

    async def test_service_assembles_same_date_stages_and_announcements_after_core_symbols(self):
        captured: dict[str, object] = {}
        providers: dict[str, object] = {}

        async def completed(*_args, **_kwargs):
            return {"status": "completed"}

        async def load_core(limit: int):
            captured["core_limit"] = limit
            return ["000001.SZ"]

        async def probe(request):
            captured["probe"] = request
            return {"status": "completed"}

        async def announcements(request):
            captured["announcements"] = request
            return {"status": "completed"}

        async def full_market_daily(request):
            captured["daily_request"] = request
            return {"status": "completed"}

        async def orchestrator(_request, **kwargs):
            captured["stage_order"] = kwargs["stage_order"]
            captured["dependencies"] = kwargs["stage_dependencies"]
            await kwargs["actions"]["full_market_daily"]()
            await kwargs["actions"]["akshare_supplements"]()
            await kwargs["actions"]["cninfo_announcements"]()
            return {"status": "completed", "stages": {}}

        dependencies = PostCloseRefreshDependencies(
            database=object(), china_today=lambda: date(2026, 8, 21), longhu_configured=lambda: False,
            longhu_close_context=lambda _day: {"status": "completed"}, provider_configs=lambda: providers,
            run_database=completed, reconcile_stale_fetch_runs=lambda *_: None,
            reprocess_remote_reports=lambda *_: None, sync_market_universe=completed,
            sync_full_market_daily=full_market_daily, sync_strategy_index_context=completed,
            build_market_snapshot=completed, load_core_symbols=load_core, akshare_probe=probe,
            sync_ths_industry_flow=completed, sync_ths_concept_flow=completed,
            rebuild_market_flow_features=lambda *_: None, refresh_pattern_sources=completed,
            persist_settled_limit_pool=lambda *_: {"status": "completed"},
            run_pattern_mining=completed, sync_daily_controls=completed,
            sync_cninfo_announcements=announcements, run_board_report=completed,
            run_strategy_decision=completed, persist_close_review=lambda *_: None,
            recompute_outcomes=lambda *_: None, recompute_intraday_outcomes=lambda *_: None,
            recompute_scorecards=lambda *_: None, rebuild_analyst_research=lambda *_: None,
            run_post_close_strategy=lambda *_: None, refresh_decision_research=lambda *_: {"status": "completed"},
            persist_watchlist_main_wave=lambda *_: None,
            build_research_snapshot=lambda *_: None, run_orchestrator=orchestrator,
            record_stage=completed, lease_key="lease", lease_seconds=lambda: 60,
            acquire_lease=lambda *_: True, renew_lease=lambda *_: True, release_lease=lambda *_: None,
            safe_error_detail=lambda value, _limit: value, json_safe=lambda value: value,
        )

        result = await run_post_close_refresh(
            PostCloseRefreshRequest(trade_date=date(2026, 8, 21), announcement_limit=7), dependencies,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured["stage_order"], POST_CLOSE_STAGE_ORDER)
        self.assertEqual(captured["dependencies"], POST_CLOSE_STAGE_DEPENDENCIES)
        self.assertEqual(captured["core_limit"], 7)
        self.assertEqual(captured["daily_request"].provider, "auto")
        self.assertEqual(captured["probe"].symbol, "000001.SZ")
        self.assertEqual(captured["announcements"].symbols, ["000001.SZ"])
        self.assertEqual(captured["announcements"].start_date, date(2026, 7, 7))

        class PromaxDaily:
            configured = True
            get_gateway_mode = "promax"

            @staticmethod
            def supports(api_name: str) -> bool:
                return api_name == "daily"

        providers["super_get"] = PromaxDaily()
        await run_post_close_refresh(
            PostCloseRefreshRequest(trade_date=date(2026, 8, 21), announcement_limit=7), dependencies,
        )
        self.assertEqual(captured["daily_request"].provider, "super_get")

    async def test_optional_stage_receipt_wrapper_is_used(self):
        seen: list[str] = []

        async def run_db(action, *args, **kwargs):
            result = action(*args)
            return await result if hasattr(result, "__await__") else result

        async def record_stage(name, day, action):
            seen.append(f"{name}:{day}")
            result = action()
            return await result if hasattr(result, "__await__") else result

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db,
            acquire_lease=lambda *_: True,
            renew_lease=lambda *_: True,
            release_lease=lambda *_: None,
            actions={"one": lambda: {"status": "completed"}}, stage_order=("one",),
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value, record_stage=record_stage,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen, ["one:2026-08-21"])

    async def test_core_daily_controls_is_a_real_post_close_stage(self):
        calls: list[str] = []

        async def run_db(action, *args, **_kwargs):
            return action(*args)

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db, acquire_lease=lambda *_: True,
            renew_lease=lambda *_: True, release_lease=lambda *_: None,
            actions={"core_daily_controls": lambda: calls.append("controls") or {"status": "completed"}},
            stage_order=("core_daily_controls",), trade_date=date(2026, 8, 21),
            safe_error_detail=lambda value, _limit: value, json_safe=lambda value: value,
        )
        self.assertEqual(calls, ["controls"])
        self.assertEqual(result["stages"]["core_daily_controls"]["status"], "completed")

    async def test_controls_execute_before_dependent_post_close_stages(self):
        calls: list[str] = []

        async def run_db(action, *args, **_kwargs):
            return action(*args)

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db, acquire_lease=lambda *_: True,
            renew_lease=lambda *_: True, release_lease=lambda *_: None,
            actions={
                "full_market_daily": lambda: calls.append("daily") or {"status": "completed"},
                "core_daily_controls": lambda: calls.append("controls") or {"status": "completed"},
                "limit_ladder": lambda: calls.append("ladder") or {"status": "completed"},
            },
            stage_order=("full_market_daily", "core_daily_controls", "limit_ladder"),
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, ["daily", "controls", "ladder"])

    async def test_dependency_gate_preserves_evidence_but_blocks_strategy_stage(self):
        calls: list[str] = []

        async def run_db(action, *args, **_kwargs):
            return action(*args)

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db, acquire_lease=lambda *_: True,
            renew_lease=lambda *_: True, release_lease=lambda *_: None,
            actions={
                "full_market_daily": lambda: calls.append("daily") or {"status": "completed"},
                "controls": lambda: calls.append("controls") or {"status": "blocked", "reason": "coverage"},
                "strategy": lambda: calls.append("strategy") or {"status": "completed"},
                "evidence": lambda: calls.append("evidence") or {"status": "completed"},
            },
            stage_order=("full_market_daily", "controls", "strategy", "evidence"),
            stage_dependencies={"strategy": ("controls",)},
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value,
        )
        self.assertEqual(calls, ["daily", "controls", "evidence"])
        self.assertEqual(result["stages"]["strategy"]["status"], "blocked")
        self.assertIn("controls", result["stages"]["strategy"]["reason"])
        self.assertFalse(result["controls_ready"])
        self.assertIn("控制面", result["retry_hint"])

    async def test_completed_stage_receipt_skips_action_after_restart(self):
        class Result:
            def fetchone(self):
                return {"run_id": "receipt-1", "status": "completed", "output_summary": {"status": "completed"}}

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Result()

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_db(action, *args, **_kwargs):
            result = action(*args)
            return await result if hasattr(result, "__await__") else result

        called = False

        def action():
            nonlocal called
            called = True
            return {"status": "completed"}

        result = await record_stage_with_receipt(
            "daily", date(2026, 8, 21), action, db=Database(),
            run_database_blocking=run_db, safe_error_detail=lambda value, _limit: value,
        )
        self.assertFalse(called)
        self.assertTrue(result["resumed_from_receipt"])

    async def test_completed_receipt_repairs_legacy_null_summary_status(self):
        class Result:
            def fetchone(self):
                return {"run_id": "receipt-1", "status": "completed", "output_summary": {"status": None}}

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Result()

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_db(action, *args, **_kwargs):
            return action(*args)

        result = await record_stage_with_receipt(
            "legacy", date(2026, 8, 21), lambda: self.fail("completed receipt reran"),
            db=Database(), run_database_blocking=run_db,
            safe_error_detail=lambda value, _limit: value,
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["resumed_from_receipt"])

    async def test_stage_failure_persists_reason_without_nameerror(self):
        # Regression: the exception bound by ``except ... as error`` is
        # deleted once the except block exits.  A lambda closing over it
        # must capture the value first, or a deferred call raises a
        # NameError instead of recording the real failure.
        class Result:
            def fetchone(self):
                return {"run_id": "receipt-2", "status": "running", "output_summary": None}

        calls: list[tuple[str, tuple]] = []

        class Connection:
            def execute(self, statement, params=None):
                calls.append((statement, params))
                return Result()

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_db(action, *args, **_kwargs):
            result = action(*args)
            return await result if hasattr(result, "__await__") else result

        def action():
            raise RuntimeError("stage exploded")

        with self.assertRaises(RuntimeError):
            await record_stage_with_receipt(
                "daily", date(2026, 8, 21), action, db=Database(),
                run_database_blocking=run_db, safe_error_detail=lambda value, _limit: value,
            )

        fail_calls = [params for statement, params in calls if "SET status='failed'" in statement]
        self.assertEqual(len(fail_calls), 1)
        self.assertEqual(fail_calls[0][1], "stage exploded")


class PostCloseStrategyStageSharesTheSchedulerRunKeyTests(unittest.IsolatedAsyncioTestCase):
    """WP6: the manual refresh's post_close_strategy stage must dedup against
    the scheduled loop, not just against its own generic stage receipt."""

    async def test_stage_calls_run_recorded_with_the_shared_task_and_run_key(self) -> None:
        from unittest.mock import patch

        from app.automation_run_repository import POST_CLOSE_STRATEGY_TASK_KEY, post_close_strategy_run_key

        captured: dict[str, object] = {}

        async def run_db(action, *args, **_kwargs):
            result = action(*args) if args else action()
            return result

        async def orchestrator(_request, **kwargs):
            captured["result"] = await kwargs["actions"]["post_close_strategy"]()
            return {"status": "completed", "stages": {}}

        async def noop(*_args, **_kwargs):
            return {"status": "completed"}

        trade_date = date(2026, 8, 21)
        dependencies = PostCloseRefreshDependencies(
            database=object(), china_today=lambda: trade_date, longhu_configured=lambda: False,
            longhu_close_context=lambda _day: {"status": "completed"}, provider_configs=lambda: {},
            run_database=run_db, reconcile_stale_fetch_runs=lambda *_: None,
            reprocess_remote_reports=lambda *_: None, sync_market_universe=noop,
            sync_full_market_daily=noop, sync_strategy_index_context=noop,
            build_market_snapshot=noop, load_core_symbols=lambda _limit: [], akshare_probe=noop,
            sync_ths_industry_flow=noop, sync_ths_concept_flow=noop,
            rebuild_market_flow_features=lambda *_: None, refresh_pattern_sources=noop,
            persist_settled_limit_pool=lambda *_: {"status": "completed"},
            run_pattern_mining=noop, sync_daily_controls=noop,
            sync_cninfo_announcements=noop, run_board_report=noop,
            run_strategy_decision=noop, persist_close_review=lambda *_: None,
            recompute_outcomes=lambda *_: None, recompute_intraday_outcomes=lambda *_: None,
            recompute_scorecards=lambda *_: None, rebuild_analyst_research=lambda *_: None,
            run_post_close_strategy=lambda _request: {"status": "completed"},
            refresh_decision_research=lambda *_: {"status": "completed"},
            persist_watchlist_main_wave=lambda *_: None,
            build_research_snapshot=lambda *_: None, run_orchestrator=orchestrator,
            record_stage=noop, lease_key="lease", lease_seconds=lambda: 60,
            acquire_lease=lambda *_: 1, renew_lease=lambda *_: 1, release_lease=lambda *_: None,
            safe_error_detail=lambda value, _limit: value, json_safe=lambda value: value,
        )

        with patch("app.post_close_refresh_service.run_recorded", return_value={"status": "completed"}) as run_recorded_mock:
            await run_post_close_refresh(
                PostCloseRefreshRequest(trade_date=trade_date, announcement_limit=7), dependencies,
            )

        run_recorded_mock.assert_called_once()
        self.assertEqual(run_recorded_mock.call_args.kwargs["task_key"], POST_CLOSE_STRATEGY_TASK_KEY)
        self.assertEqual(run_recorded_mock.call_args.kwargs["run_key"], post_close_strategy_run_key(trade_date))
        self.assertEqual(captured["result"], {"status": "completed"})


class StaleFetchRunsStageReconcilesAutomationRunsTests(unittest.IsolatedAsyncioTestCase):
    """WP6 reaper: the same stage that reconciles fetch_runs also reconciles
    orphaned automation_runs, so no second scheduled entry point is needed."""

    async def test_automation_runs_reconcile_result_is_merged_into_the_stage_payload(self) -> None:
        captured: dict[str, object] = {}

        async def run_db(action, *args, **_kwargs):
            result = action(*args) if args else action()
            return result

        async def orchestrator(_request, **kwargs):
            captured["result"] = await kwargs["actions"]["stale_fetch_runs"]()
            return {"status": "completed", "stages": {}}

        def reconcile_fetch(request):
            return {"status": "completed", "matched": 0}

        def reconcile_automation():
            captured["automation_called"] = True
            return {"status": "completed", "matched": 2}

        async def noop(*_args, **_kwargs):
            return {"status": "completed"}

        dependencies = PostCloseRefreshDependencies(
            database=object(), china_today=lambda: date(2026, 8, 21), longhu_configured=lambda: False,
            longhu_close_context=lambda _day: {"status": "completed"}, provider_configs=lambda: {},
            run_database=run_db, reconcile_stale_fetch_runs=reconcile_fetch,
            reconcile_stale_automation_runs=reconcile_automation,
            reprocess_remote_reports=lambda *_: None, sync_market_universe=noop,
            sync_full_market_daily=noop, sync_strategy_index_context=noop,
            build_market_snapshot=noop, load_core_symbols=lambda _limit: [], akshare_probe=noop,
            sync_ths_industry_flow=noop, sync_ths_concept_flow=noop,
            rebuild_market_flow_features=lambda *_: None, refresh_pattern_sources=noop,
            persist_settled_limit_pool=lambda *_: {"status": "completed"},
            run_pattern_mining=noop, sync_daily_controls=noop,
            sync_cninfo_announcements=noop, run_board_report=noop,
            run_strategy_decision=noop, persist_close_review=lambda *_: None,
            recompute_outcomes=lambda *_: None, recompute_intraday_outcomes=lambda *_: None,
            recompute_scorecards=lambda *_: None, rebuild_analyst_research=lambda *_: None,
            run_post_close_strategy=lambda *_: None, refresh_decision_research=lambda *_: {"status": "completed"},
            persist_watchlist_main_wave=lambda *_: None,
            build_research_snapshot=lambda *_: None, run_orchestrator=orchestrator,
            record_stage=noop, lease_key="lease", lease_seconds=lambda: 60,
            acquire_lease=lambda *_: 1, renew_lease=lambda *_: 1, release_lease=lambda *_: None,
            safe_error_detail=lambda value, _limit: value, json_safe=lambda value: value,
        )

        await run_post_close_refresh(
            PostCloseRefreshRequest(trade_date=date(2026, 8, 21), announcement_limit=7), dependencies,
        )

        self.assertTrue(captured["automation_called"])
        self.assertEqual(captured["result"]["stale_automation_runs"], {"status": "completed", "matched": 2})


class RunRefreshIndependentHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    """WP6: the lease must be renewed on its own clock, not only between stages.

    ``lease_seconds() / 3`` is floored at 1.0s (see run_refresh), so a stage
    that runs longer than that must still see at least one heartbeat renewal
    while it is in flight.
    """

    async def test_heartbeat_renews_while_a_single_stage_is_still_running(self) -> None:
        async def run_db(action, *args, **_kwargs):
            result = action(*args) if args else action()
            return await result if hasattr(result, "__await__") else result

        async def slow_stage() -> dict[str, object]:
            await asyncio.sleep(1.2)
            return {"status": "completed"}

        renew_calls: list[object] = []

        def renew_lease(*args):
            renew_calls.append(args)
            return 1

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 1,
            run_database_blocking=run_db, acquire_lease=lambda *_: 1,
            renew_lease=renew_lease, release_lease=lambda *_: None,
            actions={"one": slow_stage}, stage_order=("one",),
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value,
        )
        self.assertEqual(result["status"], "completed")
        # At least one renewal fired from the independent heartbeat while
        # "one" was still sleeping, plus the existing per-stage renewal.
        self.assertGreaterEqual(len(renew_calls), 2)

    async def test_heartbeat_losing_the_lease_aborts_the_whole_run(self) -> None:
        async def run_db(action, *args, **_kwargs):
            result = action(*args) if args else action()
            return await result if hasattr(result, "__await__") else result

        async def slow_first_stage() -> dict[str, object]:
            await asyncio.sleep(1.2)
            return {"status": "completed"}

        calls: list[str] = []

        def second_stage() -> dict[str, object]:
            calls.append("second")
            return {"status": "completed"}

        def renew_lease(*_args):
            # The heartbeat's very first renewal reports the lease as lost.
            return None

        with self.assertRaises(RuntimeError):
            await run_refresh(
                object(), db=object(), lease_key="lease", lease_seconds=lambda: 1,
                run_database_blocking=run_db, acquire_lease=lambda *_: 1,
                renew_lease=renew_lease, release_lease=lambda *_: None,
                actions={"one": slow_first_stage, "two": second_stage},
                stage_order=("one", "two"),
                trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
                json_safe=lambda value: value,
            )
        # "two" must never start once the heartbeat has detected the takeover.
        self.assertEqual(calls, [])


class RunRefreshLeaseFencingTests(unittest.IsolatedAsyncioTestCase):
    """WP6: run_refresh checks the acquire-time fence before each stage."""

    async def test_no_check_lease_fence_injected_preserves_existing_behavior(self) -> None:
        async def run_db(action, *args, **_kwargs):
            return action(*args)

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db, acquire_lease=lambda *_: 1,
            renew_lease=lambda *_: 1, release_lease=lambda *_: None,
            actions={"one": lambda: {"status": "completed"}}, stage_order=("one",),
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value,
        )
        self.assertEqual(result["status"], "completed")

    async def test_fence_is_checked_before_each_stage_with_the_value_captured_at_acquire(self) -> None:
        async def run_db(action, *args, **_kwargs):
            return action(*args)

        checked_fences: list[int] = []

        async def check_lease_fence(_db, _lease_key, fence):
            checked_fences.append(fence)

        result = await run_refresh(
            object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
            run_database_blocking=run_db, acquire_lease=lambda *_: 7,
            renew_lease=lambda *_: 7, release_lease=lambda *_: None,
            actions={
                "one": lambda: {"status": "completed"},
                "two": lambda: {"status": "completed"},
            },
            stage_order=("one", "two"),
            trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
            json_safe=lambda value: value, check_lease_fence=check_lease_fence,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(checked_fences, [7, 7])

    async def test_lease_lost_error_aborts_the_whole_refresh_not_just_one_stage(self) -> None:
        async def run_db(action, *args, **_kwargs):
            return action(*args)

        calls: list[str] = []

        async def check_lease_fence(_db, _lease_key, _fence):
            raise LeaseLostError("superseded")

        with self.assertRaises(LeaseLostError):
            await run_refresh(
                object(), db=object(), lease_key="lease", lease_seconds=lambda: 60,
                run_database_blocking=run_db, acquire_lease=lambda *_: 3,
                renew_lease=lambda *_: 3, release_lease=lambda *_: calls.append("released"),
                actions={
                    "one": lambda: calls.append("one") or {"status": "completed"},
                    "two": lambda: calls.append("two") or {"status": "completed"},
                },
                stage_order=("one", "two"),
                trade_date=date(2026, 8, 21), safe_error_detail=lambda value, _limit: value,
                json_safe=lambda value: value, check_lease_fence=check_lease_fence,
            )
        # Neither stage's action ran (the fence check happens first), and the
        # lease was still released on the way out.
        self.assertEqual(calls, ["released"])


class CheckPostCloseRefreshLeaseFenceWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_through_the_bounded_database_executor(self) -> None:
        from unittest.mock import patch

        import app.main as main

        calls: list[tuple] = []

        async def fake_run_database_blocking(action, *args, **kwargs):
            calls.append((action.__name__, args, kwargs))

        with patch("app.main.run_database_blocking", new=fake_run_database_blocking):
            await main.check_post_close_refresh_lease_fence(main.db, "post_close_refresh_v1", 7)

        self.assertEqual(len(calls), 1)
        name, args, kwargs = calls[0]
        self.assertEqual(name, "check_runtime_lease_fence")
        self.assertEqual(args, (main.db, "post_close_refresh_v1", 7))
        self.assertEqual(kwargs, {"timeout_seconds": 10})


class PostCloseRefreshEndpointOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """WP6: the one-click endpoints must respect QUANT_RUNTIME_PROFILE ownership."""

    async def test_refresh_endpoint_returns_409_when_profile_does_not_own_post_close_strategy(self):
        from unittest.mock import patch

        from fastapi import HTTPException

        import app.main as main

        with patch.dict("os.environ", {"QUANT_RUNTIME_PROFILE": "intraday_edge"}, clear=False), \
             patch("app.main.run_post_close_refresh") as run_refresh_mock:
            with self.assertRaises(HTTPException) as raised:
                await main.post_close_refresh_endpoint(main.PostCloseRefreshRequest())
            self.assertEqual(raised.exception.status_code, 409)
            run_refresh_mock.assert_not_called()

    async def test_start_endpoint_returns_409_when_profile_does_not_own_post_close_strategy(self):
        from unittest.mock import patch

        from fastapi import HTTPException

        import app.main as main

        with patch.dict("os.environ", {"QUANT_RUNTIME_PROFILE": "intraday_edge"}, clear=False), \
             patch("app.main.run_post_close_refresh") as run_refresh_mock:
            with self.assertRaises(HTTPException) as raised:
                await main.start_post_close_refresh_endpoint(main.PostCloseRefreshRequest())
            self.assertEqual(raised.exception.status_code, 409)
            run_refresh_mock.assert_not_called()

    async def test_refresh_endpoint_runs_when_profile_owns_post_close_strategy(self):
        from unittest.mock import AsyncMock, patch

        import app.main as main

        async def fake_run(_payload):
            return {"status": "completed"}

        for profile in ("full", "research"):
            with self.subTest(profile=profile), \
                 patch.dict("os.environ", {"QUANT_RUNTIME_PROFILE": profile}, clear=False), \
                 patch("app.main.run_post_close_refresh", new=AsyncMock(side_effect=fake_run)), \
                 patch.object(main.post_close_refresh_runtime, "run", new=AsyncMock(return_value={"status": "completed"})) as run_mock:
                result = await main.post_close_refresh_endpoint(main.PostCloseRefreshRequest())
                self.assertEqual(result, {"status": "completed"})
                run_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

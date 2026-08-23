from __future__ import annotations

import unittest
from datetime import date

from app.post_close_refresh import record_stage_with_receipt, run_refresh
from app.post_close_refresh_service import (
    POST_CLOSE_STAGE_DEPENDENCIES,
    POST_CLOSE_STAGE_ORDER,
    PostCloseRefreshDependencies,
    run_post_close_refresh,
)
from app.request_models import PostCloseRefreshRequest


class PostCloseRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_assembles_same_date_stages_and_announcements_after_core_symbols(self):
        captured: dict[str, object] = {}

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
            database=object(), china_today=lambda: date(2026, 8, 21), provider_configs=lambda: {},
            run_database=completed, reconcile_stale_fetch_runs=lambda *_: None,
            reprocess_remote_reports=lambda *_: None, sync_market_universe=completed,
            sync_full_market_daily=full_market_daily, sync_strategy_index_context=completed,
            build_market_snapshot=completed, load_core_symbols=load_core, akshare_probe=probe,
            sync_ths_industry_flow=completed, sync_ths_concept_flow=completed,
            rebuild_market_flow_features=lambda *_: None, refresh_pattern_sources=completed,
            run_pattern_mining=completed, sync_daily_controls=completed,
            sync_cninfo_announcements=announcements, run_board_report=completed,
            run_strategy_decision=completed, persist_close_review=lambda *_: None,
            recompute_outcomes=lambda *_: None, recompute_intraday_outcomes=lambda *_: None,
            recompute_scorecards=lambda *_: None, rebuild_analyst_research=lambda *_: None,
            run_post_close_strategy=lambda *_: None, persist_watchlist_main_wave=lambda *_: None,
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


if __name__ == "__main__":
    unittest.main()

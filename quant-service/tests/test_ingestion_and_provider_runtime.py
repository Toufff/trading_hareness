"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403
from app.main import _start_application_background_tasks
from app.runtime_tasks import (
    BackgroundTaskSpec,
    apply_background_runtime_profile,
    background_runtime_profile,
    background_tasks_enabled,
)


class IngestionAndProviderRuntimeTests(unittest.TestCase):
    def test_background_task_preflight_flag_defaults_on_and_explicitly_disables_leases(self):
        self.assertTrue(background_tasks_enabled({}))
        self.assertFalse(background_tasks_enabled({"QUANT_BACKGROUND_TASKS_ENABLED": "false"}))
        self.assertTrue(background_tasks_enabled({"QUANT_BACKGROUND_TASKS_ENABLED": "YES"}))

    def test_preflight_mode_creates_no_background_loop_tasks(self):
        with patch("app.main.background_tasks_enabled", return_value=False):
            self.assertEqual(_start_application_background_tasks(), {})

    def test_background_runtime_profile_defaults_to_full_and_rejects_unknown_values(self):
        self.assertEqual(background_runtime_profile({}), "full")
        self.assertEqual(background_runtime_profile({"QUANT_RUNTIME_PROFILE": "INTRADAY_EDGE"}), "intraday_edge")
        with self.assertRaisesRegex(ValueError, "QUANT_RUNTIME_PROFILE"):
            background_runtime_profile({"QUANT_RUNTIME_PROFILE": "typo"})

    def test_intraday_edge_profile_only_keeps_network_polling_loops(self):
        specs = tuple(BackgroundTaskSpec(label, True, AsyncMock()) for label in (
            "intraday_monitor", "super_get_fast_quote", "minute_profile_capture",
            "tencent_order_book", "board_flow_curve", "strategy_review",
            "post_close_strategy", "ten_day_leader_rotation", "daily_strategy_summary",
            "ths_member_backfill", "all_board_member_backfill",
        ))
        profiled = apply_background_runtime_profile(
            specs, {"QUANT_RUNTIME_PROFILE": "intraday_edge"},
        )
        self.assertEqual(
            {spec.label for spec in profiled if spec.enabled},
            {"intraday_monitor", "super_get_fast_quote", "minute_profile_capture",
             "tencent_order_book", "board_flow_curve"},
        )

    def test_research_profile_excludes_remote_owned_intraday_polling_loops(self):
        specs = (
            BackgroundTaskSpec("intraday_monitor", True, AsyncMock()),
            BackgroundTaskSpec("tencent_order_book", True, AsyncMock()),
            BackgroundTaskSpec("strategy_review", True, AsyncMock()),
            BackgroundTaskSpec("post_close_strategy", False, AsyncMock()),
        )
        profiled = apply_background_runtime_profile(specs, {"QUANT_RUNTIME_PROFILE": "research"})
        self.assertFalse(profiled[0].enabled)
        self.assertFalse(profiled[1].enabled)
        self.assertTrue(profiled[2].enabled)
        self.assertFalse(profiled[3].enabled)

    def test_generic_provider_call_uses_async_circuit_lookup_before_fallback(self):
        provider = MagicMock(key="tushare_primary")
        expected = MagicMock()

        async def check() -> AsyncMock:
            fallback = AsyncMock(return_value=expected)
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.circuit_open_provider_keys_async", new=AsyncMock(return_value={"tushare_primary"})) as lookup, \
                 patch("app.main.call_with_fallback", new=fallback):
                result = await call_tushare_api("daily", {"ts_code": "000001.SZ"}, None)
            self.assertIs(result, expected)
            lookup.assert_awaited_once_with("daily", [provider])
            return fallback

        fallback = asyncio.run(check())
        self.assertEqual(fallback.await_args.kwargs["blocked_provider_keys"], {"tushare_primary"})

    def test_tushare_daily_sync_checks_its_ledger_in_database_executor(self):
        provider = MagicMock(key="tushare_super_sdk")
        unchanged = {"status": "unchanged", "trade_date": "2026-08-11", "imported": 1, "request_key": "cached"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=unchanged)
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_tushare(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, unchanged)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["prepare_run"])

    def test_tushare_daily_sync_batches_one_provider_response_into_one_database_write(self):
        provider = MagicMock(key="tushare_super_get")
        provider_result = MagicMock(
            provider=provider, failed_providers=(), rows=[
                {"ts_code": "000001.SZ", "trade_date": "20260810", "open": 10, "high": 11, "low": 9, "close": 10.5, "pre_close": 10, "vol": 100, "amount": 1000},
                {"ts_code": "000001.SZ", "trade_date": "20260811", "open": 10.5, "high": 12, "low": 10, "close": 11.5, "pre_close": 10.5, "vol": 120, "amount": 1200},
            ],
        )

        async def check() -> tuple[dict[str, object], list[str]]:
            calls: list[str] = []

            async def blocking(operation, *args, **kwargs):
                calls.append(operation.__name__)
                return 2 if operation.__name__ == "persist_daily_bar_batch" else None

            with patch("app.main.resolve_sync_symbols_async", new=AsyncMock(return_value=["000001.SZ"])), \
                 patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.call_tushare_api", new=AsyncMock(return_value=provider_result)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_tushare(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, calls

        result, calls = asyncio.run(check())
        self.assertEqual(result["imported"], 2)
        self.assertEqual(calls, ["prepare_run", "persist_daily_bar_batch", "finalize_run"])

    def test_tushare_daily_sync_reports_shared_rate_limit_backpressure_without_provider_failure(self):
        provider = MagicMock(key="tushare_super_get")

        async def check() -> tuple[dict[str, object], list[str]]:
            calls: list[str] = []

            async def blocking(operation, *args, **kwargs):
                calls.append(operation.__name__)
                return None

            with patch("app.main.resolve_sync_symbols_async", new=AsyncMock(return_value=["000001.SZ"])), \
                 patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.call_tushare_api", new=AsyncMock(side_effect=ExecutorSaturatedError("shared provider rate-limit queue is full"))), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_tushare(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, calls

        result, calls = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failures"], [])
        self.assertEqual(len(result["local_capacity_failures"]), 1)
        self.assertEqual(calls, ["prepare_run", "finalize_run"])

    def test_daily_bar_batch_uses_one_transaction_for_all_validated_bars(self):
        connection = MagicMock()
        transaction = MagicMock()
        transaction.__enter__.return_value = connection
        bars = [
            DailyBar(symbol="000001.SZ", trading_date=date(2026, 8, 10), close=Decimal("10"), source="tushare_super_get"),
            DailyBar(symbol="000001.SZ", trading_date=date(2026, 8, 11), close=Decimal("11"), source="tushare_super_get"),
        ]
        with patch("app.main.db.transaction", return_value=transaction) as transaction_factory, \
             patch("app.main.upsert_bar") as upsert:
            stored = persist_daily_bar_batch(bars)
        self.assertEqual(stored, 2)
        transaction_factory.assert_called_once_with()
        self.assertEqual(upsert.call_count, 2)
        self.assertTrue(all(call.args[0] is connection for call in upsert.call_args_list))

    def test_baostock_daily_sync_checks_its_ledger_in_database_executor(self):
        unchanged = {"status": "unchanged", "trade_date": "2026-08-11", "imported": 1, "request_key": "cached"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=unchanged)
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())):
                result = await sync_baostock(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, unchanged)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["prepare_run"])

    def test_baostock_daily_sync_uses_the_bounded_public_source_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock, AsyncMock]:
            blocking = AsyncMock(side_effect=[None, None])
            source_executor = AsyncMock(return_value=([], ["upstream unavailable"]))
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.run_akshare_blocking", new=source_executor), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())):
                result = await sync_baostock(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, blocking, source_executor

        result, blocking, source_executor = asyncio.run(check())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(source_executor.await_count, 1)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["prepare_run", "finalize_run"])

    def test_baostock_daily_sync_batches_valid_rows_before_the_database_write(self):
        rows = [
            {"code": "sz.000001", "date": "2026-08-10", "open": "10", "high": "11", "low": "9", "close": "10.5", "preclose": "10", "volume": "100", "amount": "1000", "isST": "0"},
            {"code": "sz.000001", "date": "2026-08-11", "open": "10.5", "high": "12", "low": "10", "close": "11.5", "preclose": "10.5", "volume": "120", "amount": "1200", "isST": "0"},
        ]

        async def check() -> tuple[dict[str, object], list[str]]:
            calls: list[str] = []

            async def blocking(operation, *args, **kwargs):
                calls.append(operation.__name__)
                return 2 if operation.__name__ == "persist_daily_bar_batch" else None

            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.run_akshare_blocking", new=AsyncMock(return_value=(rows, []))), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())):
                result = await sync_baostock(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, calls

        result, calls = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(calls, ["prepare_run", "persist_daily_bar_batch", "finalize_run"])

    def test_baostock_daily_sync_skips_the_upstream_when_its_circuit_is_open(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            source_executor = AsyncMock()
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value={"daily_bar"})), \
                 patch("app.main.run_akshare_blocking", new=source_executor):
                result = await sync_baostock(TushareSyncRequest(symbols=["000001.SZ"]))
            return result, source_executor

        result, source_executor = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("circuit", str(result["reason"]))
        source_executor.assert_not_awaited()

    def test_market_universe_sync_checks_its_ledger_in_database_executor(self):
        provider = MagicMock(key="tushare_super_sdk")
        unchanged = {"status": "unchanged", "universe_key": "all_a", "imported": 1, "request_key": "cached"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=unchanged)
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_market_universe(MarketUniverseSyncRequest())
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, unchanged)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["prepare_run"])

    def test_full_market_daily_sync_checks_its_ledger_in_database_executor(self):
        provider = MagicMock(key="tushare_super_sdk")
        unchanged = {"status": "unchanged", "trade_date": "2026-08-11", "imported": 1, "request_key": "cached"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=unchanged)
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_full_market_daily(FullMarketDailySyncRequest())
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, unchanged)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["prepare_run"])

    def test_full_market_control_plane_syncs_keep_local_capacity_out_of_provider_health(self):
        provider = MagicMock(key="tushare_super_get")

        async def check() -> tuple[dict[str, object], dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[None, None, None, None])
            saturated = AsyncMock(side_effect=ExecutorSaturatedError("super_get blocking executor is saturated"))
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.call_tushare_api", new=saturated):
                universe = await sync_market_universe(MarketUniverseSyncRequest())
                daily = await sync_full_market_daily(FullMarketDailySyncRequest())
            return universe, daily, blocking

        universe, daily, blocking = asyncio.run(check())
        self.assertEqual(universe["status"], "blocked")
        self.assertEqual(daily["status"], "blocked")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "prepare_run", "persist_tushare_fetch_blocked", "prepare_run", "persist_tushare_fetch_blocked",
        ])

    def test_ths_sector_catalog_uses_database_executor_for_raw_rows_and_catalog(self):
        outcome = {"status": "completed", "request_key": "ths-index", "provider": "tushare_super_sdk"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[[{"ts_code": "885001.TI", "name": "测试板块"}], None])
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(return_value=outcome)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_ths_sector_catalog(SectorCatalogSyncRequest(index_type="N", sync_members=False))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "tushare_rows_for_request", "persist_catalog",
        ])

    def test_ths_sector_member_capacity_and_catalog_aggregation_remain_blocked(self):
        index_outcome = {"status": "completed", "request_key": "ths-index", "provider": "tushare_super_sdk"}
        capacity_error = HTTPException(status_code=503, detail="local processing capacity is temporarily saturated; retry shortly")

        async def check() -> tuple[dict[str, object], dict[str, object]]:
            blocking = AsyncMock(side_effect=[[{"ts_code": "885001.TI", "name": "测试板块"}], None])
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(side_effect=[index_outcome, capacity_error])), \
                 patch("app.main.run_database_blocking", new=blocking):
                member_result = await sync_ths_sector_catalog(SectorCatalogSyncRequest(
                    index_type="N", sync_members=True, member_limit=1,
                ))
            with patch("app.main.sync_ths_sector_catalog", new=AsyncMock(side_effect=capacity_error)):
                catalog_result = await sync_all_ths_sector_catalogs()
            return member_result, catalog_result

        member_result, catalog_result = asyncio.run(check())
        self.assertEqual(member_result["status"], "blocked")
        self.assertEqual(member_result["member_results"][0]["status"], "blocked")
        self.assertEqual(catalog_result["status"], "blocked")
        self.assertTrue(all(item["status"] == "blocked" for item in catalog_result["types"]))

    def test_isolated_sector_catalog_orchestrator_preserves_sequential_statuses(self):
        async def check():
            calls = []
            async def sync_one(request):
                calls.append(request.index_type)
                if request.index_type == "R":
                    raise HTTPException(status_code=503, detail="circuit open")
                return {"status": "completed", "sectors": 2}
            result = await isolated_sync_all_sector_catalogs(
                sync_one=sync_one, request_type=SectorCatalogSyncRequest,
                http_exception=HTTPException,
                is_local_capacity_error=lambda error: False,
                is_circuit_open_error=lambda error: "circuit" in str(error.detail),
            )
            return calls, result
        calls, result = asyncio.run(check())
        self.assertEqual(calls, ["N", "I", "R", "S", "ST", "BB"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sectors"], 10)
        self.assertEqual(result["types"][2]["status"], "circuit_open")

    def test_eastmoney_board_members_use_bounded_akshare_and_database_executors(self):
        catalog = [{"板块代码": "BK001", "板块名称": "测试概念"}]
        members = [{"代码": "000001", "名称": "测试股"}]

        async def check() -> tuple[dict[str, object], AsyncMock, AsyncMock]:
            blocking = AsyncMock(side_effect=[None, 1])
            akshare = AsyncMock(side_effect=[catalog, members])
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.run_akshare_blocking", new=akshare):
                result = await sync_eastmoney_board_members(EastmoneyBoardMemberSyncRequest(kind="concept", member_limit=1))
            return result, blocking, akshare

        result, blocking, akshare = asyncio.run(check())
        self.assertEqual(result["member_results"][0]["members"], 1)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["persist_catalog", "persist_members"])
        self.assertEqual(akshare.await_count, 2)

    def test_live_eastmoney_hydration_uses_bounded_and_database_executors(self):
        flows = [{"行业代码": "BK001", "行业": "测试概念", "流入资金": 100, "流出资金": 20}]

        async def check() -> tuple[list[dict[str, object]], AsyncMock, AsyncMock]:
            blocking = AsyncMock(side_effect=[[], 1])
            akshare = AsyncMock(return_value=[{"代码": "000001", "名称": "测试股"}])
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.run_akshare_blocking", new=akshare):
                result = await hydrate_eastmoney_live_board_members("concept", flows, 1)
            return result, blocking, akshare

        result, blocking, akshare = asyncio.run(check())
        self.assertEqual(result[0]["members"], 1)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["load_mapped_rows", "persist_members"])
        self.assertEqual(akshare.await_count, 1)

    def test_local_public_executor_saturation_is_blocked_not_a_provider_failure(self):
        async def check() -> tuple[dict[str, object], dict[str, object], tuple[dict[str, object], object], AsyncMock]:
            blocking = AsyncMock()
            saturated = AsyncMock(side_effect=ExecutorSaturatedError("public_source blocking executor is saturated"))
            async def unavailable() -> list[dict[str, object]]:
                raise ExecutorSaturatedError("public_source blocking executor is saturated")
            with patch("app.main.run_akshare_blocking", new=saturated), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_database_blocking", new=blocking):
                members = await sync_eastmoney_board_members(EastmoneyBoardMemberSyncRequest(kind="concept"))
                report = await intraday_sector_report(IntradaySectorReportRequest(kind="concept"))
                study = await stock_study_free_fetch("AKShare", "akshare", "daily_bar", unavailable, "000001.SZ")
            return members, report, study, blocking

        members, report, study, blocking = asyncio.run(check())
        self.assertEqual(members["status"], "blocked")
        self.assertIn("saturated", str(members["reason"]))
        self.assertEqual(report["status"], "blocked")
        self.assertIn("saturated", str(report["reason"]))
        self.assertEqual(study[0]["status"], "blocked")
        self.assertEqual(blocking.await_count, 0)

    def test_akshare_probe_saturation_does_not_open_the_provider_circuit(self):
        payload = AkShareProbeRequest(
            include_market_summary=False, include_lhb=False, include_strong_pool=False, include_supplements=False,
        )

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock()
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_akshare_blocking", new=AsyncMock(side_effect=ExecutorSaturatedError("public_source blocking executor is saturated"))), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await akshare_probe(payload)
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["results"][0]["status"], "blocked")
        self.assertEqual(blocking.await_count, 0)

    def test_minute_board_capture_records_local_capacity_without_provider_failure(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[None, {"status": "insufficient", "state": "insufficient"}, []])
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_akshare_blocking", new=AsyncMock(side_effect=ExecutorSaturatedError("public_source blocking executor is saturated"))), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.retry_pending_board_rotation_alerts", new=AsyncMock(return_value={"loaded": 0, "sent": 0, "failed": 0, "disabled": 0})):
                result = await capture_intraday_board_flow_curve()
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["capacity_blocks"], 2)
        self.assertEqual(result["circuit_skips"], 0)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "persist_snapshot", "persist_intraday_market_flow_feature", "evaluate_intraday_board_rotation_events",
        ])

    def test_ths_industry_moneyflow_uses_database_executor_for_rows_and_persistence(self):
        outcome = {"status": "completed", "request_key": "industry", "provider": "tushare_super_sdk"}
        rows = [{"ts_code": "885001.TI", "industry": "测试行业", "close": 1, "pct_change": 2}]

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[rows, None])
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(return_value=outcome)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_ths_industry_moneyflow(SectorFlowSyncRequest())
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["sectors"], 1)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "tushare_rows_for_request", "persist_industry_flow",
        ])

    def test_ths_concept_flows_and_strength_use_database_executor(self):
        outcomes = [
            {"status": "completed", "request_key": "concept", "provider": "tushare_super_sdk"},
            {"status": "completed", "request_key": "strength", "provider": "tushare_super_sdk"},
        ]
        concept_rows = [{"ts_code": "885001.TI", "name": "测试概念", "industry_index": 1, "pct_change": 2}]
        strength_rows = [{"ts_code": "885001.TI", "name": "测试概念", "pct_chg": 2, "cons_nums": 1}]

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[concept_rows, None, strength_rows, None])
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(side_effect=outcomes)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_ths_concept_signals(SectorFlowSyncRequest())
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "tushare_rows_for_request", "persist_concept_flow", "tushare_rows_for_request", "persist_limit_strength",
        ])

    def test_ths_concept_members_use_database_executor_for_selection_rows_and_state(self):
        selected = (date(2026, 8, 11), [{"sector_key": "885001.TI", "label": "测试概念"}], 1)
        outcome = {"status": "completed", "request_key": "member", "provider": "tushare_super_sdk"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[selected, [{"ts_code": "000001.SZ"}], 1])
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(return_value=outcome)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_ths_concept_members(ConceptMemberSyncRequest(trade_date=date(2026, 8, 11), member_limit=1))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["member_results"][0]["members"], 1)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "select_concepts", "tushare_rows_for_request", "persist_member_snapshot",
        ])

    def test_ths_concept_backfill_uses_native_async_reads_for_progress(self):
        completed = {"status": "completed", "total_concepts": 3, "member_results": []}

        async def check() -> tuple[dict[str, object], AsyncMock, AsyncMock]:
            existing = AsyncMock(return_value={"rows": 1})
            progress = AsyncMock(return_value={"done": 2, "failed": 1})
            with patch("app.main.sync_ths_concept_members", new=AsyncMock(return_value=completed)), \
                 patch("app.main.read_async_ths_concept_flow_rows", new=existing), \
                 patch("app.main.read_async_ths_concept_member_progress", new=progress):
                result = await run_ths_concept_member_backfill_batch(ConceptMemberBackfillRequest(trade_date=date(2026, 8, 11), refresh_flow_catalog=False))
            return result, existing, progress

        result, existing, progress = asyncio.run(check())
        self.assertEqual(result["progress"], {"completed_or_empty": 2, "failed": 1, "remaining": 1})
        self.assertEqual(existing.await_args.args[1], date(2026, 8, 11))
        self.assertEqual(progress.await_args.args[1], date(2026, 8, 11))

    def test_ths_catalog_member_batches_skip_non_member_index_codes(self):
        import inspect
        source = inspect.getsource(__import__("app.main", fromlist=["sync_ths_sector_catalog"]).sync_ths_sector_catalog)
        self.assertIn('re.fullmatch(r"\\d{6}\\.TI"', source)
        self.assertIn("skipped_non_member_codes", source)

    def test_concept_limit_candidates_use_database_executor_for_exact_join_and_write(self):
        selected = (date(2026, 8, 11), [{"sector_key": "885001.TI", "label": "测试概念", "net_amount": 100}])
        outcomes = [
            {"status": "completed", "request_key": "member", "provider": "tushare_super_sdk"},
            {"status": "completed", "request_key": "limit", "provider": "tushare_super_sdk"},
        ]
        limit_rows = [{"ts_code": "000001.SZ", "limit_type": "涨停池", "name": "测试股"}]

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[selected, [{"ts_code": "000001.SZ"}], 1, limit_rows, (1, [{"sector_key": "885001.TI"}])])
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(side_effect=outcomes)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_concept_limit_candidates(ConceptCandidateSyncRequest(trade_date=date(2026, 8, 11), top_concepts=1))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["candidates"], 1)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "select_concepts", "tushare_rows_for_request", "persist_members", "tushare_rows_for_request", "persist_candidates",
        ])

    def test_watchlist_history_persists_factor_snapshot_in_database_executor(self):
        factor_snapshot = {"bar_count": 21}
        source = ({"source": "watchlist", "status": "completed"}, [])

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[factor_snapshot, None])
            with patch("app.main.sync_tushare", new=AsyncMock(return_value={"status": "completed"})), \
                 patch("app.main.stock_study_fetch", new=AsyncMock(return_value=source)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await hydrate_watchlist_history(uuid.uuid4(), "000001.SZ")
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "watchlist_daily_factors", "persist_factor_snapshot",
        ])

    def test_intraday_factor_queries_reuse_the_existing_transaction_connection(self):
        class DailyConnection:
            def __init__(self) -> None:
                self.executions = 0

            def execute(self, *_args, **_kwargs):
                self.executions += 1
                return self

            def fetchall(self):
                return [{
                    "trading_date": date(2026, 7, day), "high": 10.5 + day / 10,
                    "low": 9.5 + day / 10, "close": 10 + day / 10, "volume": 1000 + day, "adj_factor": 1.0,
                } for day in range(1, 26)]

        class VolumeConnection:
            def __init__(self) -> None:
                self.executions = 0

            def execute(self, *_args, **_kwargs):
                self.executions += 1
                return self

            def fetchall(self):
                return [{"symbol": "000001.SZ", "minute_bucket": "10:00", "sample_days": 8, "median_volume": 200}]

        daily_connection, volume_connection = DailyConnection(), VolumeConnection()
        with patch("app.main.db.transaction", side_effect=AssertionError("must reuse caller connection")):
            factors = watchlist_daily_factors("000001.SZ", daily_connection)
            profile = intraday_volume_time_profile("000001.SZ", "2026-08-11 10:00:00", date(2026, 8, 11), volume_connection)
            attached = attach_intraday_volume_time_profile(
                "000001.SZ", {"time": "2026-08-11 10:00:00", "minute_volume_lot": 600},
                datetime(2026, 8, 11, 2, tzinfo=timezone.utc), volume_connection,
            )
        self.assertEqual(factors["status"], "completed")
        self.assertEqual(profile["status"], "ready")
        self.assertEqual(attached["time_bucket_volume_profile"]["volume_surprise"], 3.0)
        self.assertEqual(daily_connection.executions, 1)
        self.assertEqual(volume_connection.executions, 2)

    def test_intraday_alert_delivery_queues_before_persisting_attempt_in_database_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=None)
            create_pending = AsyncMock(return_value=uuid.uuid4())
            with patch("app.main.post_feishu_alert_text", new=AsyncMock(return_value={"status": "disabled", "reason": "not configured"})), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.create_async_pending_intraday_alert_delivery", new=create_pending):
                result = await deliver_intraday_alert(uuid.uuid4(), "测试")
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "disabled")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["persist_delivery_attempt"])

    def test_intraday_alert_retry_keeps_failed_message_outbox_bounded(self):
        from app.main import retry_pending_intraday_alerts

        due = [{"delivery_id": uuid.uuid4(), "signal_event_id": uuid.uuid4(), "message_text": "未送达提醒"}]

        async def check() -> tuple[dict[str, int], AsyncMock]:
            load_due = AsyncMock(return_value=due)
            with patch("app.main.read_async_due_intraday_alert_deliveries", new=load_due), \
                 patch("app.main.attempt_intraday_alert_delivery", new=AsyncMock(return_value={"status": "sent"})):
                return await retry_pending_intraday_alerts(limit=99), load_due

        result, load_due = asyncio.run(check())
        self.assertEqual(result, {"loaded": 1, "sent": 1, "failed": 0, "disabled": 0})
        self.assertEqual(load_due.await_args.args[1:], (3, 99))

    def test_alert_delivery_sends_auditable_recovery_receipt_after_normal_delivery_recovers(self):
        health_event = {
            "health_event_id": uuid.uuid4(), "event_type": "recovered", "streak_count": 3,
            "message_text": delivery_health_recovery_text(3),
        }

        async def check() -> tuple[dict[str, object], AsyncMock, AsyncMock]:
            blocking = AsyncMock(side_effect=[health_event, None])
            sender = AsyncMock(return_value={"status": "sent", "response": {"ok": True}})
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.post_feishu_alert_text", new=sender):
                result = await attempt_intraday_alert_delivery(uuid.uuid4(), uuid.uuid4(), "正常信号")
            return result, blocking, sender

        result, blocking, sender = asyncio.run(check())
        self.assertEqual(result["status"], "sent")
        self.assertEqual(sender.await_count, 2)
        self.assertIn("提醒通道恢复", sender.await_args_list[1].args[0])
        self.assertEqual(
            [call.args[0].__name__ for call in blocking.await_args_list],
            ["persist_delivery_attempt", "persist_health_event_attempt"],
        )

    def test_board_rotation_alert_is_frontend_only(self):
        event = {
            "rotation_event_id": uuid.uuid4(), "last_observed_at": datetime(2026, 8, 12, 1, 32, tzinfo=timezone.utc),
            "conditions": {"taxonomy_key": "eastmoney_concept", "sector_key": "CROSS", "label": "交叉概念",
                           "event_type": "cross_zero", "direction": "inflow", "previous_net_inflow": -3.0,
                           "current_net_inflow": 3.0, "delta_net_inflow": 6.0, "dynamic_threshold": 2.0, "change_pct": 1.0},
        }

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock()
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.post_feishu_alert_text", new=AsyncMock()) as outbound:
                result = await deliver_board_rotation_alert(event)
            return result, blocking, outbound

        result, blocking, outbound = asyncio.run(check())
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(blocking.await_count, 0)
        outbound.assert_not_awaited()

    def test_legacy_board_rotation_outbox_is_suppressed_without_feishu_retry(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            suppress = AsyncMock(return_value=2)
            with patch("app.main.suppress_async_legacy_board_rotation_deliveries", new=suppress), \
                 patch("app.main.post_feishu_alert_text", new=AsyncMock()) as outbound:
                result = await retry_pending_board_rotation_alerts()
            return result, outbound

        result, outbound = asyncio.run(check())
        self.assertEqual(result["suppressed"], 2)
        self.assertEqual(result["sent"], 0)
        outbound.assert_not_awaited()

    def test_daily_summary_is_persisted_for_frontend_without_external_delivery(self):
        summary = {
            "exchange_date": "2026-08-11", "signal_counts": {}, "outcome_counts": {},
            "post_close": {"status": "completed", "candidates": []},
            "readiness": {"decision_ready": False, "blockers": ["history"]},
        }

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[summary, None])
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.post_feishu_alert_text", new=AsyncMock()) as outbound:
                result = await run_daily_strategy_summary(date(2026, 8, 11))
            return result, blocking, outbound

        result, blocking, outbound = asyncio.run(check())
        self.assertEqual(result["status"], "suppressed")
        outbound.assert_not_awaited()
        self.assertEqual(
            [call.args[0].__name__ for call in blocking.await_args_list],
            ["build_daily_strategy_summary", "persist_frontend_only"],
        )

    def test_minute_session_capture_persists_in_database_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=({"000001.SZ": 0}, {}, {"000001.SZ": {"status": "completed"}}))
            with patch("app.main.realtime_market_session_async", new=AsyncMock(return_value=(True, "open"))), \
                 patch("app.main.tencent_intraday_minutes", new=AsyncMock(return_value=[])), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await capture_intraday_minute_sessions(["000001.SZ"])
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(blocking.await_args.args[0].__name__, "persist_sessions")

    def test_watchlist_upsert_uses_database_executor_before_bounded_hydration(self):
        row = {"watchlist_id": uuid.uuid4(), "symbol": "000001.SZ"}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=row)
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.hydrate_watchlist_history", new=AsyncMock(return_value={"status": "completed"})):
                result = await upsert_intraday_watchlist("000001.SZ", IntradayWatchlistRequest(symbol="000001.SZ"))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["item"], row)
        self.assertEqual(blocking.await_args.args[0].__name__, "persist_watchlist")

    def test_watchlist_delete_uses_database_executor(self):
        async def check() -> AsyncMock:
            blocking = AsyncMock(return_value={"watchlist_id": uuid.uuid4()})
            with patch("app.main.run_database_blocking", new=blocking):
                result = await delete_intraday_watchlist("000001.SZ")
            self.assertEqual(result, {"status": "deleted", "symbol": "000001.SZ"})
            return blocking

        blocking = asyncio.run(check())
        self.assertEqual(blocking.await_args.args[0].__name__, "delete_watchlist")

    def test_daily_pipeline_offloads_each_local_repository_stage(self):
        async def check() -> AsyncMock:
            blocking = AsyncMock(side_effect=[
                {"status": "ready"}, {"state": "trend_recovery"}, {"materialize_post_close_candidates": 0}, 0,
                {"outcomes": 1}, {"scorecards": 1}, {"recommendations": 1},
            ])
            # The reporting-calendar sync owns its own provider call and its
            # own persist transaction, so it is stubbed here rather than being
            # allowed to consume one of the offload side effects below.
            with patch("app.main.sync_tushare", new=AsyncMock(return_value={"status": "completed"})), \
                 patch("app.main.sync_tushare_daily_core", new=AsyncMock(return_value={"status": "completed"})), \
                 patch("app.main.sync_earnings_calendar", new=AsyncMock(return_value={"status": "completed"})), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await run_daily_pipeline(GenerateRequest())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["earnings_calendar"], {"status": "completed"})
            return blocking

        blocking = asyncio.run(check())
        self.assertEqual(
            [call.args[0].__name__ for call in blocking.await_args_list],
            ["build_snapshot", "materialize_market_regime_today", "materialize_strategy_daily_candidate_ledger",
             "materialize_daily_watchlist_proposals", "recompute_outcomes", "recompute_scorecards", "generate_recommendations"],
        )

    def test_post_close_refresh_returns_conflict_when_durable_lease_is_held(self):
        async def check() -> tuple[HTTPException, AsyncMock]:
            blocking = AsyncMock(return_value=False)
            with patch("app.main.run_database_blocking", new=blocking):
                with self.assertRaises(HTTPException) as raised:
                    await run_post_close_refresh(PostCloseRefreshRequest())
            return raised.exception, blocking

        error, blocking = asyncio.run(check())
        self.assertEqual(error.status_code, 409)
        self.assertIn("another service instance", str(error.detail))
        self.assertEqual(blocking.await_args.args[0].__name__, "acquire_runtime_lease")

    def test_async_sse_calendar_gate_fails_closed_when_local_calendar_is_missing(self):
        async def check() -> tuple[bool, bool]:
            with patch("app.main.read_async_sse_calendar_open", new=AsyncMock(side_effect=[False, True])):
                return (
                    await sse_calendar_open_async(date(2026, 8, 11)),
                    await sse_calendar_open_async(date(2026, 8, 12)),
                )
        self.assertEqual(asyncio.run(check()), (False, True))

    def test_sync_session_gates_fail_closed_when_the_trade_calendar_has_a_gap(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        context = MagicMock()
        context.__enter__.return_value = connection
        during_continuous_auction = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
        during_board_observation = datetime(2026, 8, 11, 1, 25, tzinfo=timezone.utc)
        with patch("app.main.db.transaction", return_value=context):
            realtime_active, realtime_reason = realtime_market_session(now=during_continuous_auction)
            board_active, board_reason = intraday_board_curve_session(now=during_board_observation)
        self.assertFalse(realtime_active)
        self.assertFalse(board_active)
        self.assertIn("fail closed", realtime_reason)
        self.assertIn("fail closed", board_reason)

    def test_async_realtime_session_gate_uses_native_async_calendar_and_fails_closed(self):
        during_continuous_auction = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)

        async def check() -> tuple[tuple[bool, str], tuple[bool, str]]:
            with patch("app.main.read_async_realtime_market_session", new=AsyncMock(side_effect=[
                (False, "SSE trade calendar has no entry for today; fail closed"),
                (True, "SSE continuous auction session"),
            ])):
                return (
                    await realtime_market_session_async(now=during_continuous_auction),
                    await realtime_market_session_async(now=during_continuous_auction),
                )

        missing, open_day = asyncio.run(check())
        self.assertFalse(missing[0])
        self.assertIn("fail closed", missing[1])
        self.assertTrue(open_day[0])

    def test_async_calendar_gates_fail_closed_when_database_executor_is_saturated(self):
        during_continuous_auction = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
        during_board_observation = datetime(2026, 8, 11, 1, 25, tzinfo=timezone.utc)

        async def check() -> tuple[bool, tuple[bool, str], tuple[bool, str]]:
            reason = "local calendar unavailable; fail closed: database pool unavailable"
            with patch("app.main.read_async_sse_calendar_open", new=AsyncMock(return_value=False)), \
                 patch("app.main.read_async_realtime_market_session", new=AsyncMock(return_value=(False, reason))), \
                 patch("app.main.read_async_sse_calendar_status", new=AsyncMock(return_value=(False, reason))):
                return (
                    await sse_calendar_open_async(date(2026, 8, 11)),
                    await realtime_market_session_async(now=during_continuous_auction),
                    await intraday_board_curve_session_async(now=during_board_observation),
                )

        calendar_open, realtime, board = asyncio.run(check())
        self.assertFalse(calendar_open)
        self.assertFalse(realtime[0])
        self.assertIn("local calendar unavailable", realtime[1])
        self.assertFalse(board[0])
        self.assertIn("local calendar unavailable", board[1])

    def test_intraday_sector_report_runs_local_membership_join_in_database_executor(self):
        local_report = [{"taxonomy_key": "eastmoney_concept", "sector_key": "BK001", "label": "测试概念",
                         "net_inflow": 123.0, "change_pct": 1.2, "mapped_members": 1,
                         "quoted_members": 1, "top_stocks": []}]

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=(local_report, {"concept": {"flow_boards": 1, "boards_with_members": 1}}, [], [], []))
            with patch("app.main.run_akshare_blocking", new=AsyncMock(return_value=[
                {"板块名称": "测试概念", "流入资金": 200, "流出资金": 77},
            ])), patch("app.main.fuyao_all_a_snapshot_rows", new=AsyncMock(return_value=(
                [{"symbol": "000001.SZ", "name": "测试股", "pct_change": 1.2, "turnover": 10}], {"status": "fresh"},
            ))), patch("app.main.run_database_blocking", new=blocking):
                return await intraday_sector_report(IntradaySectorReportRequest(kind="concept", top_stocks=10)), blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"], local_report)
        self.assertEqual(blocking.await_count, 1)
        self.assertEqual(blocking.await_args.args[0].__name__, "build_intraday_sector_report_from_membership")

    def test_pattern_mining_uses_database_executor_without_replaying_an_empty_sample(self):
        selection = {"status": "blocked", "candidates": [], "cohort_counts": {}, "limit_pool_rows": 0, "limit_step_rows": 0}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[date(2026, 8, 11), selection, "run-123"])
            with patch("app.main.run_database_blocking", new=blocking):
                result = await run_strategy_pattern_mining(StrategyPatternMiningRequest(refresh_limit_sources=False))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["run_id"], "run-123")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "latest_strategy_pattern_date", "strategy_pattern_sample_candidates", "persist_strategy_pattern_run",
        ])

    def test_pattern_mining_skips_tencent_minute_replay_when_its_circuit_is_open(self):
        candidate = {
            "symbol": "000001.SZ", "name": "测试股", "primary_cohort": "limit_pool", "cohorts": ["limit_pool"],
            "board_context": {}, "limit_context": {}, "daily_features": {}, "risk_flags": [],
        }
        selection = {"status": "completed", "candidates": [candidate], "cohort_counts": {"limit_pool": 1},
                     "limit_pool_rows": 1, "limit_step_rows": 0}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[date(2026, 8, 11), selection, "run-124"])
            minute_fetch = AsyncMock()
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value={"intraday_minute"})), \
                 patch("app.main.tencent_intraday_minutes", new=minute_fetch):
                result = await run_strategy_pattern_mining(StrategyPatternMiningRequest(refresh_limit_sources=False))
            return result, minute_fetch

        result, minute_fetch = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["source_status"]["minute"]["status"], "circuit_open")
        minute_fetch.assert_not_awaited()

    def test_intraday_peer_minutes_skip_upstream_when_circuit_is_open(self):
        watches = [{"symbol": "000001.SZ", "metadata": {"surge_strategy": {"enabled": True, "peer_symbols": []}}}]

        async def check() -> tuple[dict[str, object], dict[str, object], AsyncMock]:
            minute_fetch = AsyncMock()
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value={"intraday_minute"})), \
                 patch("app.main.tencent_intraday_minutes", new=minute_fetch), \
                 patch("app.main._intraday_tencent_minute_cache", new={}):
                features, source = await intraday_tencent_surge_context(watches)
            return features, source, minute_fetch

        features, source, minute_fetch = asyncio.run(check())
        self.assertEqual(features, {})
        self.assertEqual(source["provider_status"], "circuit_open")
        minute_fetch.assert_not_awaited()

    def test_market_snapshot_uses_database_executor_when_public_refresh_is_disabled(self):
        expected = {"status": "blocked", "universe_count": 1, "quote_count": 0, "coverage": 0.0}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[["000001.SZ"], expected])
            with patch("app.main.run_database_blocking", new=blocking):
                result = await build_market_snapshot(MarketSnapshotRequest(session="close", refresh_public_quotes=False))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, expected)
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "snapshot_universe_symbols", "finalize_market_snapshot",
        ])

    def test_market_snapshot_skips_circuit_open_public_providers_without_external_requests(self):
        expected = {"status": "blocked", "source_summary": {"tencent_snapshot": {"status": "circuit_open"}}}

        async def check() -> tuple[dict[str, object], AsyncMock, AsyncMock]:
            blocking = AsyncMock(side_effect=[["000001.SZ"], expected])
            circuits = AsyncMock(return_value={"realtime_quote"})
            with patch("app.main.market_snapshot_thresholds", return_value=(1, 0.95, set())), \
                 patch("app.main.market_snapshot_public_quote_settings", return_value={"enabled": True, "batch_size": 80, "concurrency": 2}), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.open_provider_capabilities", new=circuits), \
                 patch("app.main.run_akshare_blocking", new=AsyncMock()) as upstream:
                result = await build_market_snapshot(MarketSnapshotRequest(session="close", refresh_public_quotes=True))
            return result, circuits, upstream

        result, circuits, upstream = asyncio.run(check())
        self.assertEqual(result, expected)
        self.assertEqual(circuits.await_count, 2)
        upstream.assert_not_awaited()

    def test_cninfo_sync_skips_when_its_provider_circuit_is_open(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            circuit = AsyncMock(return_value={"announcement"})
            with patch("app.main.open_provider_capabilities", new=circuit), \
                 patch("app.main.cninfo_announcements", new=AsyncMock()) as upstream:
                result = await sync_cninfo_announcements(AnnouncementSyncRequest(symbols=["000001.SZ"]))
            return result, upstream

        result, upstream = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        upstream.assert_not_awaited()

    def test_cninfo_sync_persists_events_and_health_in_database_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[0, None])
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())), \
                 patch("app.main.cninfo_announcements", new=AsyncMock(return_value=[])), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await sync_cninfo_announcements(AnnouncementSyncRequest(symbols=["000001.SZ"]))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "persist_market_events", "persist_announcement_provider_health",
        ])

    def test_post_close_refresh_constructs_a_valid_cninfo_date_range_and_research_wrapper(self):
        source = Path("app/post_close_refresh_service.py").read_text()
        composition = Path("app/main.py").read_text()
        self.assertIn('start_date=trade_date - timedelta(days=45)', source)
        self.assertIn('rebuild_analyst_research=rebuild_analyst_research_for_date', composition)
        self.assertIn('def rebuild_analyst_research_for_date(as_of_date: date)', composition)

    def test_akshare_probe_persists_each_probe_step_in_database_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=0)
            disabled = {
                "include_market_summary": False, "include_lhb": False, "include_strong_pool": False,
                "include_supplements": False, "include_moneyflow": False, "include_limit_pools": False,
                "include_lhb_supplements": False, "include_block_trades": False, "include_corporate_risk": False,
                "include_analyst_heat": False, "include_index_fund": False,
            }
            with patch("app.main.run_akshare_blocking", new=AsyncMock(return_value=[])), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())):
                result = await akshare_probe(AkShareProbeRequest(**disabled))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["results"][0]["capability"], "daily_bar")
        self.assertEqual(blocking.await_args.args[0].__name__, "persist_akshare_probe_result")

    def test_akshare_probe_skips_the_upstream_when_capability_circuit_is_open(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            source_executor = AsyncMock()
            disabled = {
                "include_market_summary": False, "include_lhb": False, "include_strong_pool": False,
                "include_supplements": False, "include_moneyflow": False, "include_limit_pools": False,
                "include_lhb_supplements": False, "include_block_trades": False, "include_corporate_risk": False,
                "include_analyst_heat": False, "include_index_fund": False,
            }
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value={"daily_bar"})), \
                 patch("app.main.run_akshare_blocking", new=source_executor):
                result = await akshare_probe(AkShareProbeRequest(**disabled))
            return result, source_executor

        result, source_executor = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["results"][0]["status"], "circuit_open")
        source_executor.assert_not_awaited()

    def test_stock_study_free_fetch_persists_public_evidence_in_database_executor(self):
        async def fetcher() -> list[dict[str, object]]:
            return []

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=0)
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())):
                source, _ = await stock_study_free_fetch("test", "tencent_free", "daily_bar", fetcher, "000001.SZ")
            return source, blocking

        source, blocking = asyncio.run(check())
        self.assertEqual(source["status"], "empty")
        self.assertEqual(blocking.await_args.args[0].__name__, "persist_stock_study_free_result")

    def test_stock_study_free_fetch_skips_uncreated_request_when_circuit_is_open(self):
        fetcher = MagicMock()

        async def check() -> dict[str, object]:
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value={"daily_bar"})):
                source, payload = await stock_study_free_fetch("test", "tencent_free", "daily_bar", fetcher, "000001.SZ")
            self.assertEqual(payload, [])
            return source

        source = asyncio.run(check())
        self.assertEqual(source["status"], "circuit_open")
        fetcher.assert_not_called()

    def test_background_task_observer_consumes_failure_and_removes_task(self):
        async def fails() -> None:
            raise RuntimeError("expected task failure")

        async def check() -> tuple[set[asyncio.Task[object]], MagicMock]:
            task = asyncio.create_task(fails())
            await asyncio.sleep(0)
            in_flight: set[asyncio.Task[object]] = {task}
            reporter = MagicMock()
            with patch("builtins.print", reporter):
                observe_completed_task(task, in_flight, "test")
            return in_flight, reporter

        in_flight, reporter = asyncio.run(check())
        self.assertFalse(in_flight)
        self.assertIn("test task failed", reporter.call_args.args[0])

    def test_loop_supervisor_restarts_after_failure_and_preserves_cancellation(self):
        async def check() -> int:
            starts = 0
            second_started = asyncio.Event()

            async def loop() -> None:
                nonlocal starts
                starts += 1
                if starts == 1:
                    raise RuntimeError("expected startup failure")
                second_started.set()
                await asyncio.Event().wait()

            registry = LoopRuntimeRegistry()
            task = asyncio.create_task(supervise_loop("test_loop", loop, restart_delay_seconds=0.01, on_state=registry.mark))
            await asyncio.wait_for(second_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return starts, registry.snapshot()

        with patch("builtins.print"):
            starts, states = asyncio.run(check())
        self.assertEqual(starts, 2)
        self.assertEqual(states["test_loop"]["state"], "stopped")
        self.assertIsNone(states["test_loop"]["last_error"])

    def test_loop_registry_keeps_only_bounded_local_lifecycle_details(self):
        registry = LoopRuntimeRegistry()
        registry.mark("test", "failed", "x" * 500)
        registry.mark("test", "running")
        item = registry.snapshot()["test"]
        self.assertEqual(item["state"], "running")
        self.assertIsNone(item["last_error"])
        self.assertIn("updated_at", item)

    def test_lifespan_task_registry_starts_only_enabled_unique_loops_and_cleans_them_up(self):
        async def check() -> tuple[list[str], dict[str, asyncio.Task[None]]]:
            started = asyncio.Event()
            labels: list[str] = []

            async def factory() -> None:
                started.set()
                await asyncio.Event().wait()

            async def run_leased(label: str, loop_factory):
                labels.append(label)
                await loop_factory()

            tasks = start_leased_background_tasks((
                BackgroundTaskSpec("enabled", True, factory),
                BackgroundTaskSpec("disabled", False, factory),
            ), run_leased)
            await asyncio.wait_for(started.wait(), timeout=1)
            await cancel_background_tasks(tasks)
            return labels, tasks

        labels, tasks = asyncio.run(check())
        self.assertEqual(labels, ["enabled"])
        self.assertEqual(set(tasks), {"enabled"})
        self.assertEqual(tasks["enabled"].get_name(), "background-loop:enabled")
        self.assertTrue(tasks["enabled"].cancelled())

    def test_lifespan_task_registry_rejects_duplicate_lease_labels_before_starting(self):
        async def noop() -> None:
            return None

        async def run_leased(_label: str, _factory) -> None:
            return None

        with self.assertRaisesRegex(ValueError, "unique"):
            start_leased_background_tasks((
                BackgroundTaskSpec("duplicate", True, noop),
                BackgroundTaskSpec("duplicate", False, noop),
            ), run_leased)

    def test_loop_supervisor_keeps_restarting_after_more_than_one_failure(self):
        async def check() -> int:
            starts = 0
            third_started = asyncio.Event()

            async def loop() -> None:
                nonlocal starts
                starts += 1
                if starts < 3:
                    raise RuntimeError("transient failure")
                third_started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(supervise_loop("test_loop_many", loop, restart_delay_seconds=0.01))
            await asyncio.wait_for(third_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return starts

        with patch("builtins.print"):
            self.assertEqual(asyncio.run(check()), 3)

    def test_leased_loop_stops_worker_and_releases_when_renewal_is_lost(self):
        async def check() -> tuple[int, int]:
            starts = 0
            releases = 0
            worker_started, released = asyncio.Event(), asyncio.Event()

            async def factory() -> None:
                nonlocal starts
                starts += 1
                worker_started.set()
                await asyncio.Event().wait()

            acquired_once = False
            async def acquire() -> bool:
                nonlocal acquired_once
                if acquired_once:
                    return False
                acquired_once = True
                return True

            async def renew() -> bool:
                return False

            async def release() -> None:
                nonlocal releases
                releases += 1
                released.set()

            task = asyncio.create_task(supervise_leased_loop(
                "lease_test", factory, acquire, renew, release, lease_seconds=3, retry_delay_seconds=0.01,
            ))
            await asyncio.wait_for(worker_started.wait(), timeout=1)
            await asyncio.wait_for(released.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return starts, releases

        with patch("builtins.print"):
            self.assertEqual(asyncio.run(check()), (1, 1))

    def test_leased_loop_retries_acquire_and_contains_control_plane_errors(self):
        async def check() -> tuple[int, int]:
            acquire_calls = 0
            releases = 0
            started, release_attempted = asyncio.Event(), asyncio.Event()

            async def factory() -> None:
                started.set()
                await asyncio.Event().wait()

            async def acquire() -> bool:
                nonlocal acquire_calls
                acquire_calls += 1
                if acquire_calls == 1:
                    raise RuntimeError("database momentarily unavailable")
                return acquire_calls == 2

            async def renew() -> bool:
                raise RuntimeError("renew failed")

            async def release() -> None:
                nonlocal releases
                releases += 1
                release_attempted.set()
                raise RuntimeError("release failed")

            task = asyncio.create_task(supervise_leased_loop(
                "lease_error_test", factory, acquire, renew, release, lease_seconds=3, retry_delay_seconds=0.01,
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(release_attempted.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return acquire_calls, releases

        with patch("builtins.print"):
            acquire_calls, releases = asyncio.run(check())
        self.assertGreaterEqual(acquire_calls, 2)
        self.assertEqual(releases, 1)

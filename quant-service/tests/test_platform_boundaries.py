"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


def _full_market_request(*, trade_date=None):
    """Stand-in for FullMarketDailySyncRequest in pipeline wiring tests."""
    return {"trade_date": trade_date}


class PlatformBoundaryTests(unittest.TestCase):
    def test_post_close_refresh_orchestrator_continues_after_stage_failure_and_releases_lease(self):
        calls: list[str] = []
        lease = AsyncMock(side_effect=[True, True, True, True])
        release = AsyncMock()
        blocking = AsyncMock(side_effect=[True, True, True, True, True, True, True])

        async def failing_stage() -> dict[str, object]:
            calls.append("failed")
            raise RuntimeError("provider unavailable")

        async def later_stage() -> dict[str, object]:
            calls.append("later")
            return {"status": "completed", "value": 1}

        async def check() -> dict[str, object]:
            return await run_refresh(
                object(), db=object(), lease_key="post-close", lease_seconds=lambda: 30,
                run_database_blocking=blocking, acquire_lease=lambda *_args: True,
                renew_lease=lambda *_args: True, release_lease=lambda *_args: None,
                actions={"failed": failing_stage, "later": later_stage}, stage_order=("failed", "later"),
                trade_date=date(2026, 8, 14), safe_error_detail=lambda value, _limit: value,
                json_safe=lambda value: value,
            )

        result = asyncio.run(check())
        self.assertEqual(calls, ["failed", "later"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["deferred_stages"], ["failed"])

    def test_daily_pipeline_blocks_before_outcomes_when_snapshot_quality_is_not_ready(self):
        async def sync(_payload):
            return {"status": "completed"}

        blocking = AsyncMock(return_value={"status": "blocked"})

        async def check() -> dict[str, object]:
            return await run_pipeline(
                GenerateRequest(), sync_full_market_daily=sync, sync_baostock=sync,
                sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
                tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
                snapshot_request=lambda as_of: {"as_of_date": as_of},
                build_snapshot=MagicMock(), recompute_outcomes=MagicMock(), recompute_scorecards=MagicMock(),
                generate_recommendations=MagicMock(), run_database_blocking=blocking, cn_today=lambda: date(2026, 8, 14),
            )

        result = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(blocking.await_count, 1)

    def test_daily_pipeline_materializes_regime_and_ledger_before_settling_outcomes(self):
        async def sync(_payload):
            return {"status": "completed"}

        build_snapshot, materialize_regime, materialize_ledger, materialize_proposals = object(), object(), object(), object()
        recompute_outcomes, recompute_scorecards, generate_recommendations = object(), object(), object()
        canned = {
            build_snapshot: {"status": "ready"}, materialize_regime: {"state": "trend_recovery"},
            materialize_ledger: {"materialize_post_close_candidates": 3}, materialize_proposals: 5,
            recompute_outcomes: {"outcomes": 1},
            recompute_scorecards: {"scorecards": 1}, generate_recommendations: {"recommendations": 1},
        }
        call_order: list[object] = []

        async def blocking(operation, *_args, **_kwargs):
            call_order.append(operation)
            return canned[operation]

        async def check() -> dict[str, object]:
            return await run_pipeline(
                GenerateRequest(), sync_full_market_daily=sync, sync_baostock=sync,
                sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
                tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
                snapshot_request=lambda as_of: {"as_of_date": as_of},
                build_snapshot=build_snapshot, recompute_outcomes=recompute_outcomes,
                recompute_scorecards=recompute_scorecards, generate_recommendations=generate_recommendations,
                run_database_blocking=blocking, cn_today=lambda: date(2026, 8, 14),
                materialize_regime=materialize_regime, materialize_candidate_ledger=materialize_ledger,
                materialize_watchlist_proposals=materialize_proposals,
            )

        result = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["regime"], {"state": "trend_recovery"})
        self.assertEqual(result["candidate_ledger"], {"materialize_post_close_candidates": 3})
        self.assertEqual(result["watchlist_proposals"], 5)
        # Regime and ledger must materialize before outcomes settle against them.
        self.assertLess(call_order.index(materialize_regime), call_order.index(recompute_outcomes))
        self.assertLess(call_order.index(materialize_ledger), call_order.index(recompute_outcomes))
        # Proposals must be read after the ledger materializes (they read from it).
        self.assertLess(call_order.index(materialize_ledger), call_order.index(materialize_proposals))

    def test_post_close_evidence_aggregation_keeps_exact_board_and_deduplicates_lhb(self):
        boards = exact_board_context([
            {"symbol": "000001.SZ", "net_amount": 10, "label": "A"},
            {"symbol": "000001.SZ", "net_amount": 20, "label": "B"},
        ], json_safe=lambda value: value)
        self.assertEqual(boards["000001.SZ"]["label"], "B")
        rows = [{"api_name": "top_inst", "provider_key": "tushare", "available_at": None,
                 "row_data": {"ts_code": "000001.SZ", "exalter": "机构", "buy": 10, "sell": 4}},
                {"api_name": "top_inst", "provider_key": "tushare", "available_at": None,
                 "row_data": {"ts_code": "000001.SZ", "exalter": "机构", "buy": 10, "sell": 4}}]
        lhb = lhb_context(rows, number=lambda value: float(value or 0))
        self.assertEqual(lhb["000001.SZ"]["institution_records"], 1)
        self.assertEqual(lhb["000001.SZ"]["institution_net_buy"], 6.0)

    def test_intraday_outcome_settlement_entry_delegates_to_isolated_repository(self):
        import app.main as main_module
        sentinel = {"status": "settled"}
        with patch("app.main.persist_intraday_outcome_settlement", return_value=sentinel) as settle:
            with patch("app.main.intraday_outcome_cutoff") as cutoff:
                with patch("app.main.refresh_intraday_signal_attributions", return_value=7) as backfill:
                    with patch("app.main.invalidate_intraday_probability_profiles") as invalidate:
                        cutoff.return_value = datetime(2026, 8, 13, tzinfo=timezone.utc)
                        result = main_module.recompute_intraday_signal_outcomes(date(2026, 8, 13))
        self.assertEqual(result, {"status": "settled", "attribution_backfilled": 7})
        settle.assert_called_once()
        backfill.assert_called_once()
        invalidate.assert_called_once()

    def test_intraday_attribution_refresh_is_bounded_to_settleable_signal_states(self):
        import app.main as main_module

        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        changed = main_module.refresh_intraday_signal_attributions(
            connection, cutoff=datetime(2026, 8, 13, tzinfo=timezone.utc),
        )

        self.assertEqual(changed, 0)
        query, params = connection.execute.call_args.args
        self.assertIn("state IN ('confirmed','alerted')", query)
        self.assertIn("signal_type IN ('entry','watch','reduce','exit')", query)
        self.assertEqual(params, (datetime(2026, 8, 13, tzinfo=timezone.utc),))

    def test_post_close_candidate_screen_is_pure_and_fail_closed_on_coverage(self):
        blocked = screen_candidates(
            date(2026, 8, 13), 10, 3, 2, [], {},
            daily_base_structure=lambda rows: {}, forming_structure=lambda rows: {},
            fresh_start_structure=lambda rows: {},
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["candidates"], [])

    def test_post_close_candidate_screen_prefers_ready_base_and_keeps_provisional_flag(self):
        rows = [{"symbol": symbol, "name": name, "close": 10}
                for symbol, name in (("000001.SZ", "A"), ("000002.SZ", "B")) for _ in range(30)]
        result = screen_candidates(
            date(2026, 8, 13), 10, 2, 2, rows,
            {"000001.SZ": {"net_amount": 1, "flow_percentile": 1}},
            daily_base_structure=lambda rows: {"status": "ready", "score": 80, "quality_flags": []},
            forming_structure=lambda rows: {"status": "forming", "score": 70, "quality_flags": []},
            fresh_start_structure=lambda rows: {"status": "not_ready", "score": 0, "quality_flags": []},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["candidates"][0]["candidate_type"], "base_ready_30d")
        self.assertEqual(result["summary"]["base_ready_30d"], 2)
        self.assertEqual(len(result["screen_observations"]), 2)
        self.assertEqual({item["screen_state"] for item in result["screen_observations"]}, {"candidate"})
    def test_intraday_terminal_repository_records_failure_latency(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        scan_id = uuid.uuid4()

        with patch("app.intraday_scan_repository.record_provider_failure") as record_failure:
            persist_intraday_scan_terminal(
                database, scan_id, datetime(2026, 8, 13, 3, tzinfo=timezone.utc), "completed",
                ["600000.SH"], {"tencent": "unavailable"}, {"watched": 1},
                provider_failure="upstream timeout", provider_latency_ms=234,
            )

        record_failure.assert_called_once_with(
            connection, "tencent_free", "realtime_quote", "upstream timeout", 234,
        )
        sql = connection.execute.call_args.args[0]
        self.assertIn("INSERT INTO quant.intraday_scan_runs", sql)
        self.assertEqual(connection.execute.call_args.args[1][0], scan_id)

    def test_market_session_repository_fails_closed_without_calendar_row(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        active, reason = read_market_session(
            database, now=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(active)
        self.assertIn("no entry", reason)
        self.assertEqual(connection.execute.call_args.args[1], (date(2026, 8, 13),))

    async def _run_market_session_async(self):
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"is_open": True}
        async def runner(action, *args, **kwargs):
            return action(*args)
        active, reason = await read_market_session_async(
            database, now=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc), database_runner=runner,
        )
        self.assertTrue(active)
        self.assertIn("session", reason)

    def test_market_session_repository_async_keeps_calendar_offload_contract(self):
        asyncio.run(self._run_market_session_async())

    def test_sse_calendar_repository_async_fails_closed_for_gap_and_executor_pressure(self):
        database = MagicMock()

        async def missing(_action, *args, **kwargs):
            return None

        async def saturated(_action, *args, **kwargs):
            raise ExecutorSaturatedError("database blocking executor is saturated")

        self.assertFalse(asyncio.run(read_sse_calendar_open_async(
            database, date(2026, 8, 13), database_runner=missing,
        )))
        self.assertFalse(asyncio.run(read_sse_calendar_open_async(
            database, date(2026, 8, 13), database_runner=saturated,
        )))

    def test_sse_calendar_status_exposes_the_same_fail_closed_reason_to_all_consumers(self):
        database = MagicMock()

        async def missing(_action, *args, **kwargs):
            return None

        async def saturated(_action, *args, **kwargs):
            raise ExecutorSaturatedError("database blocking executor is saturated")

        gap = asyncio.run(read_sse_calendar_status_async(database, date(2026, 8, 13), database_runner=missing))
        pressure = asyncio.run(read_sse_calendar_status_async(database, date(2026, 8, 13), database_runner=saturated))
        self.assertEqual(gap, (False, "SSE trade calendar has no entry for today; fail closed"))
        self.assertFalse(pressure[0])
        self.assertIn("local calendar capacity unavailable", pressure[1])

    def test_runtime_tushare_rate_limits_are_mirrored_without_secrets(self):
        connection = MagicMock()
        primary = MagicMock(key="tushare_primary", rate_limit_per_minute=61)
        super_get = MagicMock(key="tushare_super_get", rate_limit_per_minute=17)

        sync_runtime_provider_rate_limits(connection, {"primary": primary, "super_get": super_get})

        self.assertEqual(connection.execute.call_count, 4)
        first_rate_update = connection.execute.call_args_list[0]
        self.assertEqual(first_rate_update.args[1], (61, "tushare_primary"))
        first_metadata_update = connection.execute.call_args_list[1]
        self.assertEqual(first_metadata_update.args[1], (61, "tushare_primary"))
        self.assertIn("runtime_environment", first_metadata_update.args[0])

    def test_metrics_refreshes_local_circuit_gauge_without_provider_io(self):
        import app.main as main_module

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"count": 3}
        context = MagicMock()
        context.__enter__.return_value = connection
        original = main_module._metrics_control_plane_refreshed_at
        main_module._metrics_control_plane_refreshed_at = 0.0
        try:
            with patch("app.main.db.pool_status", return_value={"pool_size": 2, "available": 1, "waiting": 0}), \
                 patch("app.main.db.transaction", return_value=context), \
                 patch("app.main.db_pool_connections") as pool_metric, \
                 patch("app.main.provider_circuit_open") as circuit_metric:
                self.assertTrue(main_module.refresh_metrics_control_plane(now=10.0))
                self.assertFalse(main_module.refresh_metrics_control_plane(now=11.0))
            self.assertEqual(pool_metric.labels.call_count, 3)
            circuit_metric.set.assert_called_once_with(3)
            self.assertIn("circuit_open_until", connection.execute.call_args.args[0])
        finally:
            main_module._metrics_control_plane_refreshed_at = original

    def test_provider_actions_router_has_only_bounded_post_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_provider_actions_router(ProviderActionDependencies(
            akshare_probe=action, realtime_probe=action, tushare_audit=action,
            tushare_fetch=action, stock_study=action,
        ))
        methods_by_path: dict[str, set[str]] = {}
        for route in router.routes:
            methods_by_path.setdefault(route.path, set()).update(route.methods or set())
        self.assertEqual(methods_by_path["/api/v1/providers/akshare/probe"], {"POST"})
        self.assertEqual(methods_by_path["/api/v1/providers/realtime/probe"], {"POST"})
        self.assertEqual(methods_by_path["/api/v1/providers/tushare/audit"], {"POST"})
        self.assertEqual(methods_by_path["/api/v1/providers/tushare/fetch"], {"POST"})
        self.assertEqual(methods_by_path["/api/v1/providers/fuyao/query"], {"POST"})
        self.assertEqual(methods_by_path["/api/v1/stocks/{symbol}/study"], {"POST"})

    def test_async_sync_symbol_resolution_uses_native_async_repository(self):
        async def check() -> AsyncMock:
            core_symbols = AsyncMock(return_value=["600519.SH"])
            with patch.dict("os.environ", {"QUANT_UNIVERSE": ""}, clear=False), \
                 patch("app.main.read_async_core_symbols", new=core_symbols):
                symbols = await resolve_sync_symbols_async([])
            self.assertEqual(symbols, ["000300.SH", "600519.SH"])
            return core_symbols

        core_symbols = asyncio.run(check())
        core_symbols.assert_awaited_once()

    def test_market_actions_router_has_explicit_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_market_actions_router(MarketActionDependencies(
            import_bars=MagicMock(return_value={"imported": 0}), sync_universe=action,
            sync_full_daily=action, sync_full_daily_controls=action, post_close_refresh=action,
            start_post_close_refresh=AsyncMock(return_value={"status": "running"}), sync_announcements=action,
            rebuild_market_flow_features=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/market/bars/import", "/api/v1/market/universe/sync",
            "/api/v1/market/sync/full-daily", "/api/v1/market/sync/full-daily-controls",
            "/api/v1/market/post-close/refresh", "/api/v1/market/post-close/refresh/start",
            "/api/v1/events/cninfo/sync", "/api/v1/market/flow/features/rebuild",
        ):
            self.assertEqual(methods_by_path[path], {"POST"})

    def test_intraday_actions_router_has_only_explicit_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_intraday_actions_router(IntradayActionDependencies(
            upsert_watchlist=action, sync_watchlist_history=action,
            delete_watchlist=AsyncMock(return_value={"status": "deleted"}),
            scan_watchlist=action, capture_minute_sessions=action,
            board_report=action, close_board_report=action,
        ))
        methods_by_path: dict[str, set[str]] = {}
        for route in router.routes:
            methods_by_path.setdefault(route.path, set()).update(route.methods or set())
        expected = {
            "/api/v1/intraday/watchlists/{symbol}": {"PUT", "DELETE"},
            "/api/v1/intraday/watchlists/{symbol}/history/sync": {"POST"},
            "/api/v1/intraday/scan": {"POST"},
            "/api/v1/intraday/minute-sessions/capture": {"POST"},
            "/api/v1/intraday/board-report/run": {"POST"},
            "/api/v1/market/sectors/review/report/run": {"POST"},
        }
        for path, methods in expected.items():
            self.assertEqual(methods_by_path[path], methods)

    def test_sector_actions_router_has_explicit_bounded_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_sector_actions_router(SectorActionDependencies(
            sync_catalog=action, sync_eastmoney_members=action, intraday_report=action,
            sync_industry_flows=action, sync_concepts=action, sync_concept_members=action,
            backfill_concept_members=action, sync_concept_candidates=action, run_board_research=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/market/sectors/sync",
            "/api/v1/market/sectors/eastmoney/members/sync",
            "/api/v1/market/sectors/intraday/report",
            "/api/v1/market/sectors/flows/sync",
            "/api/v1/market/sectors/concepts/sync",
            "/api/v1/market/sectors/concepts/members/sync",
            "/api/v1/market/sectors/concepts/members/backfill/run",
            "/api/v1/market/sectors/concepts/candidates/sync",
            "/api/v1/market/sectors/concepts/research/run",
        ):
            self.assertEqual(methods_by_path[path], {"POST"})

    def test_strategy_actions_router_has_explicit_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_strategy_actions_router(StrategyActionDependencies(
            decision=action, review=action, post_close=action, pattern_mining=action,
            watchlist_main_wave=action,
            recompute_scorecards=action, recompute_outcomes=action,
            recompute_intraday_outcomes=action, generate_recommendations=action, daily_pipeline=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/strategy/decisions/run", "/api/v1/strategy/reviews/run",
            "/api/v1/strategy/post-close/run", "/api/v1/strategy/pattern-mining/run",
            "/api/v1/strategy/watchlist-main-wave/run",
            "/api/v1/analyst-scorecards/recompute", "/api/v1/outcomes/recompute",
            "/api/v1/intraday/outcomes/recompute", "/api/v1/recommendations/generate",
            "/api/v1/pipeline/daily",
        ):
            self.assertEqual(methods_by_path[path], {"POST"})

    def test_research_actions_router_has_only_local_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_research_actions_router(ResearchActionDependencies(
            analyse_ingestion=action, import_remote_report=action, import_remote_message=action,
            reprocess_remote_reports=action, reprocess_remote_messages=action,
            review_claim=action, update_universe=action, build_features=action,
            evaluate_factors=action, backtest=action, reconcile_fetch_runs=action, build_snapshot=action,
            update_analyst_research_profile=action,
            update_analyst_sync_cursor=action,
            update_analyst_global_sync_cursor=action,
            sync_remote_archive=action,
            replay_recorded_intraday_events=action,
            replay_recorded_rule_inputs=action,
            run_entry_timing_challengers=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/analysis/jobs/{analysis_id}/run",
            "/api/v1/remote-archive/reports/import",
            "/api/v1/remote-archive/reports/reprocess",
            "/api/v1/remote-archive/messages/import",
            "/api/v1/remote-archive/messages/reprocess",
            "/api/v1/remote-archive/sync",
            "/api/v1/strategies/intraday/replay-recorded-events",
            "/api/v1/strategies/intraday/replay-recorded-inputs",
            "/api/v1/strategies/intraday/entry-timing-challengers",
            "/api/v1/claim-review/{review_id}",
            "/api/v1/universes/members", "/api/v1/features/build",
            "/api/v1/factors/evaluate", "/api/v1/strategies/backtest",
            "/api/v1/operations/fetch-runs/reconcile-stale", "/api/v1/data-snapshots/build",
            "/api/v1/analyst-research/profiles/{analyst_id}",
        ):
            self.assertEqual(methods_by_path[path], {"PUT"} if path.endswith("/{analyst_id}") else {"POST"})
        self.assertEqual(methods_by_path["/api/v1/remote-archive/sync-cursors-global"], {"PUT"})

    def test_ingestion_actions_router_has_explicit_bounded_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_ingestion_actions_router(IngestionActionDependencies(
            market_snapshot=action, import_offline_minutes=action, sync_tushare=action,
            sync_baostock=action, sync_tushare_core=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/market/snapshots/run", "/api/v1/market/minute/import-offline",
            "/api/v1/market/sync/tushare", "/api/v1/market/sync/baostock",
            "/api/v1/market/sync/tushare/core",
        ):
            self.assertEqual(methods_by_path[path], {"POST"})

    def test_post_close_structure_exports_share_the_side_effect_free_module(self):
        self.assertIs(daily_base_structure, pure_daily_base_structure)
        self.assertIs(post_close_forming_structure, pure_post_close_forming_structure)
        self.assertIs(post_close_fresh_start_structure, pure_post_close_fresh_start_structure)

    def test_intraday_runtime_status_evidence_is_a_bounded_read_only_repository_query(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        query_results = [
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchone=MagicMock(return_value={"last_observed_at": None, "rows": 0, "latest_trading_date": None})),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchone=MagicMock(return_value={"total": 0, "v2": 0, "v1": 0, "latest_observed_at": None})),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchone=MagicMock(return_value={"count": 0})),
            MagicMock(fetchone=MagicMock(return_value={"count": 0})),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchone=MagicMock(return_value=None)),
            MagicMock(fetchone=MagicMock(return_value={"enabled": 2})),
        ]
        connection.execute.side_effect = query_results

        evidence = load_intraday_runtime_evidence(database, 3)

        self.assertEqual(evidence["pending_delivery_count"], 0)
        self.assertEqual(evidence["pending_rotation_delivery_count"], 0)
        self.assertEqual(evidence["watch_row"], {"enabled": 2})
        self.assertIsNone(evidence["latest_health_event"])
        self.assertEqual(connection.execute.call_count, 16)
        self.assertEqual(evidence["minute_profile"]["rows"], 0)
        self.assertIn("attempt_count<%s", connection.execute.call_args_list[11].args[0])

    def test_intraday_status_read_model_is_local_and_dependency_injected(self):
        evidence = {
            "health_rows": [], "quote_rows": [], "raw_rows": [],
            "minute_profile": {"last_observed_at": None, "rows": 0, "latest_trading_date": None}, "latest_scan": None,
            "latest_completed_scan": None, "latest_board": None, "latest_board_curve": None,
            "latest_delivery": None, "delivery_history": [], "pending_delivery_count": 0,
            "pending_rotation_delivery_count": 0, "latest_daily_summary": None,
            "latest_health_event": None, "watch_row": {"enabled": 0},
        }
        database = MagicMock()
        dependencies = IntradayStatusDependencies(
            database=database, alert_max_attempts=3,
            realtime_market_session=lambda: (False, "closed"), board_curve_session=lambda: (False, "closed"),
            high_frequency_window=lambda _: False, scan_interval_seconds=lambda: 30,
            provider_status=lambda: [{"name": "primary", "configured": True}, {"name": "super_get", "configured": True}],
            runtime_service_state=lambda **_: ("standby", None), json_safe=lambda value: value,
            super_get_fast_interval_seconds=lambda: 1.0, super_get_fast_max_in_flight=lambda: 20,
            fast_quote_retention_days=lambda: 7, board_curve_enabled=lambda: True,
            board_curve_retention_days=lambda: 60, board_rotation_retention_days=lambda: 60,
            daily_summary_automation_enabled=lambda: True,
            order_book_max_symbols=lambda: 40,
        )
        with patch("app.intraday_status_read_model.load_intraday_runtime_evidence", return_value=evidence):
            payload = read_intraday_services_status_payload(dependencies)

        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual([item["key"] for item in payload["items"]][-1], "primary_realtime")
        self.assertEqual(next(item for item in payload["items"] if item["key"] == "primary_realtime")["state"], "unavailable")
        order_book = next(item for item in payload["items"] if item["key"] == "tencent_order_book")
        self.assertEqual(order_book["details"]["max_symbols"], 40)
        self.assertEqual(order_book["details"]["uncovered_watch_count"], 0)
        database.transaction.assert_not_called()

    def test_intraday_status_projects_city_sdk_rt_min_health_before_legacy_identity(self):
        observed_at = datetime.now(timezone.utc)
        evidence = {
            "health_rows": [
                {"provider_key": "tushare_super_get", "capability": "rt_min", "updated_at": observed_at - timedelta(minutes=2),
                 "last_success_at": None, "last_failure_at": observed_at - timedelta(minutes=2), "last_error": "gateway error"},
                {"provider_key": "tushare_super_sdk", "capability": "rt_min", "updated_at": observed_at - timedelta(minutes=1),
                 "last_success_at": observed_at - timedelta(minutes=1), "last_failure_at": None, "last_error": None},
            ],
            "quote_rows": [], "raw_rows": [{"api_name": "rt_min", "last_observed_at": observed_at, "rows": 3}],
            "minute_profile": {}, "latest_scan": None, "latest_completed_scan": None,
            "latest_board": None, "latest_board_curve": None, "latest_delivery": None, "delivery_history": [],
            "pending_delivery_count": 0, "pending_rotation_delivery_count": 0, "latest_daily_summary": None,
            "latest_health_event": None, "watch_row": {"enabled": 1},
        }
        dependencies = IntradayStatusDependencies(
            database=MagicMock(), alert_max_attempts=3,
            realtime_market_session=lambda: (False, "closed"), board_curve_session=lambda: (False, "closed"),
            high_frequency_window=lambda _: False, scan_interval_seconds=lambda: 30,
            provider_status=lambda: [{"name": "super_get", "configured": True}],
            runtime_service_state=lambda **_: ("standby", None), json_safe=lambda value: value,
            super_get_fast_interval_seconds=lambda: 1.0, super_get_fast_max_in_flight=lambda: 20,
            fast_quote_retention_days=lambda: 7, board_curve_enabled=lambda: True,
            board_curve_retention_days=lambda: 60, board_rotation_retention_days=lambda: 60,
            daily_summary_automation_enabled=lambda: True, order_book_max_symbols=lambda: 40,
        )

        payload = read_intraday_services_status_payload(dependencies, evidence=evidence)
        minute = next(item for item in payload["items"] if item["key"] == "super_rt_min")
        self.assertEqual(minute["last_success_at"], observed_at - timedelta(minutes=1))
        self.assertEqual(minute["details"]["provider_order"], ["tushare_super_sdk", "tushare_super_get"])
        self.assertEqual(minute["details"]["health_provider_key"], "tushare_super_sdk")

    def test_intraday_status_degrades_when_fresh_direct_watch_quotes_do_not_cover_pool(self):
        observed_at = datetime.now(timezone.utc)
        evidence = {
            "health_rows": [], "quote_rows": [{"source_name": "fuyao_ths", "last_observed_at": observed_at, "rows": 5}], "raw_rows": [],
            "minute_profile": {"last_observed_at": None, "rows": 0, "latest_trading_date": None},
            "latest_scan": {"status": "completed", "observed_at": observed_at, "source_status": {}, "summary": {}},
            "latest_completed_scan": {"status": "completed", "observed_at": observed_at, "summary": {}, "source_status": {
                "fuyao": {"all_a_only_watch_quote_symbols": 1, "all_a_snapshot": {"status": "fresh", "age_seconds": 0}},
                "tencent_watch": {"decision_eligible_watch_quote_symbols": 1,
                            "sina_fallback_watch_quote_symbols": 0, "quote_timestamp_slo_seconds": 20},
            }},
            "latest_board": None, "latest_board_curve": None, "latest_delivery": None, "delivery_history": [],
            "pending_delivery_count": 0, "pending_rotation_delivery_count": 0, "latest_daily_summary": None,
            "latest_health_event": None, "watch_row": {"enabled": 2},
        }
        dependencies = IntradayStatusDependencies(
            database=MagicMock(), alert_max_attempts=3,
            realtime_market_session=lambda: (True, "open"), board_curve_session=lambda: (True, "open"),
            high_frequency_window=lambda _: True, scan_interval_seconds=lambda: 30,
            provider_status=lambda: [{"name": "primary", "configured": True}, {"name": "super_get", "configured": True}],
            runtime_service_state=lambda **_: ("healthy", 1.0), json_safe=lambda value: value,
            super_get_fast_interval_seconds=lambda: 1.0, super_get_fast_max_in_flight=lambda: 20,
            fast_quote_retention_days=lambda: 7, board_curve_enabled=lambda: True,
            board_curve_retention_days=lambda: 60, board_rotation_retention_days=lambda: 60,
            daily_summary_automation_enabled=lambda: True, order_book_max_symbols=lambda: 40,
        )
        payload = read_intraday_services_status_payload(dependencies, evidence=evidence, session=(True, "open"), board_session=(True, "open"))
        order_book = next(item for item in payload["items"] if item["key"] == "tencent_order_book")
        self.assertEqual(order_book["state"], "degraded")
        self.assertIn("1/2", order_book["last_error"])
        self.assertTrue(payload["summary"]["decision_path_degraded"])

    def test_intraday_status_projects_and_gates_stale_public_flow_snapshot(self):
        observed_at = datetime.now(timezone.utc)
        evidence = {
            "health_rows": [], "quote_rows": [{"source_name": "fuyao_ths", "last_observed_at": observed_at, "rows": 2}], "raw_rows": [],
            "minute_profile": {"last_observed_at": None, "rows": 0, "latest_trading_date": None},
            "latest_scan": {"status": "completed", "observed_at": observed_at, "source_status": {}, "summary": {}},
            "latest_completed_scan": {"status": "completed", "observed_at": observed_at, "summary": {}, "source_status": {
                "fuyao": {"all_a_only_watch_quote_symbols": 0,
                            "all_a_snapshot": {"status": "cached", "age_seconds": 46.0, "ttl_seconds": 30.0}},
                "tencent_watch": {"decision_eligible_watch_quote_symbols": 2,
                                  "sina_fallback_watch_quote_symbols": 0},
            }},
            "latest_board": None, "latest_board_curve": None, "latest_delivery": None, "delivery_history": [],
            "pending_delivery_count": 0, "pending_rotation_delivery_count": 0, "latest_daily_summary": None,
            "latest_health_event": None, "watch_row": {"enabled": 2},
        }
        dependencies = IntradayStatusDependencies(
            database=MagicMock(), alert_max_attempts=3,
            realtime_market_session=lambda: (True, "open"), board_curve_session=lambda: (True, "open"),
            high_frequency_window=lambda _: True, scan_interval_seconds=lambda: 30,
            provider_status=lambda: [{"name": "primary", "configured": True}, {"name": "super_get", "configured": True}],
            runtime_service_state=lambda **_: ("healthy", 1.0), json_safe=lambda value: value,
            super_get_fast_interval_seconds=lambda: 1.0, super_get_fast_max_in_flight=lambda: 20,
            fast_quote_retention_days=lambda: 7, board_curve_enabled=lambda: True,
            board_curve_retention_days=lambda: 60, board_rotation_retention_days=lambda: 60,
            daily_summary_automation_enabled=lambda: True, order_book_max_symbols=lambda: 40,
        )

        payload = read_intraday_services_status_payload(dependencies, evidence=evidence, session=(True, "open"), board_session=(True, "open"))

        fuyao = next(item for item in payload["items"] if item["key"] == "fuyao_ths_realtime")
        self.assertEqual(fuyao["state"], "degraded")
        self.assertEqual(fuyao["details"]["snapshot"]["max_decision_age_seconds"], 45.0)
        self.assertIn("Fuyao all-A", fuyao["last_error"])
        self.assertTrue(payload["summary"]["decision_path_degraded"])

    def test_health_read_model_uses_only_injected_local_dependencies(self):
        database = MagicMock()
        database.pool_status.return_value = {"pool_size": 2, "available": 1, "waiting": 0}
        connection = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        open_circuits = MagicMock()
        open_circuits.fetchone.return_value = {"count": 2}
        post_close = MagicMock()
        post_close.fetchone.return_value = {"expires_at": "later", "updated_at": "now"}
        loops = MagicMock()
        loops.fetchall.return_value = [{"lease_key": "background_loop:intraday_monitor", "expires_at": "later", "updated_at": "now"}]
        connection.execute.side_effect = [open_circuits, post_close, loops]
        pool_updates: list[dict[str, object]] = []
        circuit_updates: list[int] = []
        dependencies = HealthDependencies(
            database=database, post_close_lease_key="post-close", data_directory=lambda: Path("/tmp"),
            background_loop_lease_seconds=lambda: 120,
            resource_status=lambda _: {"state": "healthy"}, public_http_client_status=lambda: {"active": True},
            alert_http_client_status=lambda: {"active": True}, provider_http_client_status=lambda: {"active": True},
            remote_archive_http_client_status=lambda: {"active": True},
            network_status=lambda: {"state": "online"},
            provider_request_reservation_status=lambda: {"shared_database_reservation": True},
            runtime_executor_status=lambda: {"database": {"occupied": 0}}, super_get_executor_status=lambda: {"occupied": 0},
            provider_status=lambda: [{"name": "super_get"}], free_provider_status=lambda: [{"name": "tencent"}],
            realtime_market_session=lambda: (False, "closed"), board_curve_session=lambda: (False, "closed"),
            scan_interval_seconds=lambda: 30, effective_scan_interval_seconds=lambda interval, _: interval,
            high_frequency_window=lambda _: False, super_get_fast_interval_seconds=lambda: 1.0,
            super_get_fast_max_in_flight=lambda: 20, fast_quote_retention_days=lambda: 7,
            board_curve_enabled=lambda: True, board_curve_retention_days=lambda: 60,
            board_rotation_retention_days=lambda: 60, set_db_pool_gauge=pool_updates.append,
            set_open_circuit_gauge=circuit_updates.append,
            background_loop_status=lambda: {"intraday_monitor": {"state": "running", "updated_at": "now", "last_error": None}},
            optional_background_tasks=lambda: {
                "background_tasks_enabled": True,
                "background_loop:ths_member_backfill": False,
            },
            daily_control_plane_status=lambda: {"state": "ready", "trade_date": "2026-08-21"},
            live_session_acceptance_status=lambda: {"state": "passed", "checked_at": "2026-08-24T10:00:00Z"},
            release_metadata=lambda: {"git_sha": "a1b2c3d", "release": "edge-test", "build_created_at": "2026-08-24T12:00:00Z"},
            post_close_runtime_status=lambda: {"active_count": 1, "oldest_started_at": "now"},
        )
        payload = read_health_payload(dependencies)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["runtime_leases"]["background_loop_lease_seconds"], 120)
        self.assertTrue(payload["provider_rate_limits"]["shared_database_reservation"])
        self.assertEqual(payload["runtime_leases"]["background_loops"][0]["lease_key"], "background_loop:intraday_monitor")
        self.assertEqual(payload["runtime_loops"]["intraday_monitor"]["state"], "running")
        self.assertEqual(payload["runtime_loops"]["intraday_monitor"]["lease_heartbeat_at"], "now")
        self.assertEqual(payload["runtime_loops"]["intraday_monitor"]["lease_expires_at"], "later")
        self.assertEqual(payload["optional_background_tasks"], {
            "background_tasks_enabled": True,
            "background_loop:ths_member_backfill": False,
        })
        self.assertEqual(payload["daily_control_plane"]["state"], "ready")
        self.assertEqual(payload["live_session_acceptance"]["state"], "passed")
        self.assertEqual(payload["runtime_tasks"]["post_close_refresh"]["active_count"], 1)
        self.assertEqual(payload["build"]["git_sha"], "a1b2c3d")
        self.assertEqual(pool_updates, [{"pool_size": 2, "available": 1, "waiting": 0}])
        self.assertEqual(circuit_updates, [2])

        database.ping.side_effect = RuntimeError("database down")
        with self.assertRaises(DatabaseUnavailableError):
            read_health_payload(dependencies)

    def test_http_clients_are_reused_only_inside_the_service_lifecycle(self):
        async def check() -> tuple[bool, bool, bool, bool, bool, bool, int, int, bool]:
            await close_http_clients()
            await start_http_clients()
            async with public_http_client() as first, public_http_client() as second:
                reused = first is second
            async with alert_http_client() as first_alert, alert_http_client() as second_alert:
                alert_reused = first_alert is second_alert
            async with provider_http_client("tushare_super_sdk", "http://proxy.example:8080") as first_provider, \
                    provider_http_client("tushare_super_sdk", "http://proxy.example:8080") as second_provider:
                provider_reused = first_provider is second_provider
            async with remote_archive_http_client("https://archive.example", None) as first_archive, \
                    remote_archive_http_client("https://archive.example", None) as second_archive:
                archive_reused = first_archive is second_archive
            active_before_close = bool(public_http_client_status()["lifecycle_owned"])
            alert_active_before_close = bool(alert_http_client_status()["lifecycle_owned"])
            active_provider_pools = int(provider_http_client_status()["active_provider_pools"])
            active_archive_pools = int(remote_archive_http_client_status()["active_archive_pools"])
            await close_http_clients()
            active_after_close = bool(public_http_client_status()["lifecycle_owned"])
            return (reused, alert_reused, provider_reused, archive_reused, active_before_close, alert_active_before_close,
                    active_provider_pools, active_archive_pools, active_after_close)

        (reused, alert_reused, provider_reused, archive_reused, active_before_close, alert_active_before_close,
         active_provider_pools, active_archive_pools, active_after_close) = asyncio.run(check())
        self.assertTrue(reused)
        self.assertTrue(alert_reused)
        self.assertTrue(provider_reused)
        self.assertTrue(archive_reused)
        self.assertTrue(active_before_close)
        self.assertTrue(alert_active_before_close)
        self.assertEqual(active_provider_pools, 1)
        self.assertEqual(active_archive_pools, 1)
        self.assertFalse(active_after_close)
        self.assertFalse(alert_http_client_status()["lifecycle_owned"])
        self.assertEqual(provider_http_client_status()["active_provider_pools"], 0)
        self.assertEqual(remote_archive_http_client_status()["active_archive_pools"], 0)

    def test_provider_health_presentation_distinguishes_configuration_circuit_and_failure(self):
        observed_at = datetime(2026, 8, 11, 3, tzinfo=timezone.utc)
        circuit = provider_health_item(
            {"enabled": True, "circuit_open_until": datetime(2026, 8, 11, 3, 5, tzinfo=timezone.utc)},
            configured=True, observed_at=observed_at,
        )
        failed = provider_health_item(
            {"enabled": True, "last_success_at": datetime(2026, 8, 11, 2, tzinfo=timezone.utc),
             "last_failure_at": datetime(2026, 8, 11, 2, 30, tzinfo=timezone.utc)},
            configured=True, observed_at=observed_at,
        )
        unconfigured = provider_health_item({"enabled": True}, configured=False, observed_at=observed_at)
        self.assertEqual(circuit["state"], "circuit_open")
        self.assertEqual(failed["state"], "degraded")
        self.assertEqual(unconfigured["state"], "unconfigured")
        self.assertEqual(provider_health_summary([circuit, failed, unconfigured])["degraded"], 1)

    def test_city_rt_k_is_delayed_context_not_verified_realtime(self):
        self.assertNotIn("rt_k", SUPER_SDK_REALTIME_APIS)
        self.assertIn("rt_k", SUPER_SDK_DELAYED_CONTEXT_APIS)
        configs = provider_configs({
            "TUSHARE_SUPER_SDK_TOKEN": "sdk", "TUSHARE_SUPER_SDK_API_URL": "https://city.example",
            "TUSHARE_SUPER_GET_API_KEY": "get", "TUSHARE_SUPER_GET_API_URL": "https://get.example",
        })
        self.assertEqual([provider.name for provider in provider_candidates("rt_k", "super", environ={
            "TUSHARE_SUPER_SDK_TOKEN": "sdk", "TUSHARE_SUPER_SDK_API_URL": "https://city.example",
            "TUSHARE_SUPER_GET_API_KEY": "get", "TUSHARE_SUPER_GET_API_URL": "https://get.example",
        })], ["super_get"])
        city = next(item for item in provider_status(environ={
            "TUSHARE_SUPER_SDK_TOKEN": "sdk", "TUSHARE_SUPER_SDK_API_URL": "https://city.example",
            "TUSHARE_SUPER_GET_API_KEY": "get", "TUSHARE_SUPER_GET_API_URL": "https://get.example",
        }) if item["name"] == "super_sdk")
        self.assertIn("rt_k", city["delayed_context_apis"])
        self.assertNotIn("rt_k", city["realtime_apis"])

    def test_provider_health_snapshot_reads_only_stored_evidence(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"provider_key": "tushare_super_get", "enabled": True, "capability": "rt_k", "market": "cn",
             "circuit_open_until": None, "last_success_at": None, "last_failure_at": None},
        ]
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        snapshot = provider_health_snapshot(
            database, [{"provider_key": "tushare_super_get", "configured": True}],
            datetime(2026, 8, 11, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(snapshot["items"][0]["state"], "unknown")
        self.assertEqual(connection.execute.call_count, 1)

    def test_provider_status_router_keeps_catalog_and_health_as_read_only_routes(self):
        router = build_provider_status_router(MagicMock(), lambda: [], lambda: [])
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/providers/tushare/catalog"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/providers/capabilities"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/providers/health"], {"GET"})

    def test_strategy_pattern_read_model_and_router_are_local_get_only(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        payload = read_latest_strategy_pattern_mining(
            database, lambda *_: {"items": [], "coverage": {}}, lambda _: 0, lambda value: value,
            lambda _: {}, lambda _: {}, lambda _: {},
        )
        self.assertIsNone(payload["run"])
        self.assertEqual(connection.execute.call_count, 1)
        router = build_strategy_pattern_reads_router(
            database, lambda *_: {"items": [], "coverage": {}}, lambda _: 0, lambda value: value,
            lambda _: {}, lambda _: {}, lambda _: {},
        )
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/strategy/pattern-mining/latest"], {"GET"})

    def test_research_readiness_router_keeps_estimates_and_frameworks_read_only(self):
        router = build_research_readiness_router(
            MagicMock(), lambda request: {"years": request.years}, lambda _connection: {}, lambda _database: {},
        )
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/research-frameworks"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/training/roadmap"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/data-readiness/history-estimate"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/data-readiness/features"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/data-readiness/replay"], {"GET"})
        self.assertEqual(training_roadmap_payload()["status"], "planned")

    def test_replay_readiness_keeps_p2_and_p3_gates_explicit(self):
        blocked = replay_readiness_payload({
            "full_cross_section_days": 16, "offline_minute_trading_days": 0,
            "offline_minute_symbols": 0, "offline_minute_bars": 0,
            "completed_offline_imports": 0, "confirmed_signal_events": 3, "matured_signal_events": 1,
        })
        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse(blocked["p2_data_foundation_ready"])
        self.assertFalse(blocked["p3_strategy_validation_ready"])
        self.assertIn("does not call providers", blocked["policy"])
        self.assertEqual(blocked["forward_capture"]["status"], "accumulating")

        unclocked_minutes = replay_readiness_payload({
            "full_cross_section_days": P2_MIN_FULL_CROSS_SECTION_DAYS,
            "first_full_cross_section_date": "2023-08-15", "latest_full_cross_section_date": "2026-08-14",
            "offline_minute_trading_days": P3_MIN_REPLAY_DAYS,
            "offline_minute_symbols": 10, "offline_minute_bars": 10_000,
            "completed_offline_imports": 1, "confirmed_signal_events": P3_MIN_SIGNAL_EVENTS,
            "matured_signal_events": P3_MIN_SIGNAL_EVENTS,
        })
        availability_gate = next(item for item in unclocked_minutes["gates"]
                                 if item["key"] == "p2_offline_minute_availability_clock")
        self.assertEqual(availability_gate["status"], "insufficient")
        self.assertFalse(unclocked_minutes["p2_data_foundation_ready"])

        ready = replay_readiness_payload({
            "full_cross_section_days": P2_MIN_FULL_CROSS_SECTION_DAYS,
            "first_full_cross_section_date": "2023-08-15", "latest_full_cross_section_date": "2026-08-14",
            "offline_minute_trading_days": P3_MIN_REPLAY_DAYS,
            "offline_minute_symbols": 10, "offline_minute_bars": 10_000,
            "offline_minute_source_clock_bars": 10_000,
            "offline_minute_source_clock_days": P3_MIN_REPLAY_DAYS,
            "forward_rule_input_days": P3_MIN_REPLAY_DAYS,
            "forward_rule_input_rows": 100_000,
            "completed_offline_imports": 1, "confirmed_signal_events": P3_MIN_SIGNAL_EVENTS,
            "matured_signal_events": P3_MIN_SIGNAL_EVENTS,
        })
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["p2_data_foundation_ready"])
        self.assertTrue(ready["p3_strategy_validation_ready"])
        self.assertEqual(ready["forward_capture"]["status"], "ready")

        short_span = replay_readiness_payload({
            "full_cross_section_days": P2_MIN_FULL_CROSS_SECTION_DAYS,
            "first_full_cross_section_date": "2025-01-01", "latest_full_cross_section_date": "2025-12-31",
        })
        span_gate = next(item for item in short_span["gates"] if item["key"] == "p2_daily_calendar_span")
        self.assertEqual(span_gate["required"], P2_MIN_DAILY_CALENDAR_SPAN_DAYS)
        self.assertEqual(span_gate["status"], "insufficient")

    def test_research_catalog_read_model_and_router_bound_local_result_sets(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        connection.execute.side_effect = [MagicMock(fetchone=MagicMock(return_value=None))]
        self.assertEqual(read_latest_features(database, "core", 10_000), {"snapshot": None, "items": []})
        self.assertIn("LIMIT 1", connection.execute.call_args.args[0])
        connection.execute.side_effect = [MagicMock(fetchall=MagicMock(return_value=[]))]
        self.assertEqual(read_factor_evaluations(database, "core", 10_000)["items"], [])
        self.assertEqual(connection.execute.call_args.args[1], ("core", 500))
        connection.execute.side_effect = [MagicMock(fetchall=MagicMock(return_value=[]))]
        self.assertEqual(read_strategy_experiments(database, "core", 10_000)["items"], [])
        self.assertEqual(connection.execute.call_args.args[1], ("core", 200))
        connection.execute.side_effect = [MagicMock(fetchall=MagicMock(return_value=[]))]
        self.assertEqual(read_data_quality_issues(database, 10_000)["items"], [])
        self.assertEqual(connection.execute.call_args.args[1], (500,))
        router = build_research_catalog_reads_router(database)
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/universes/{universe_key}"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/features/latest"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/factors"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/factors/evaluations"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/strategies"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/strategies/experiments"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/data-quality/issues"], {"GET"})

    def test_intraday_status_router_keeps_the_runtime_panel_read_only(self):
        router = build_intraday_status_router(lambda: {"items": []})
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/intraday/services/status"], {"GET"})

    def test_intraday_outcome_read_model_batches_context_and_router_is_get_only(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        observed_at = datetime(2026, 8, 10, 1, tzinfo=timezone.utc)
        connection.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[{
                "signal_event_id": "event-1", "horizon_key": "5m", "symbol": "600000.SH",
                "signal_key": "watchlist-confirmation-v4", "signal_type": "watch", "observed_at": observed_at,
                "conditions": {}, "evidence": {}, "status": "matured", "raw_return": 0.01,
            }])),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        batch_calls: list[list[tuple[datetime, str]]] = []
        payload = read_latest_intraday_outcomes(
            database, 10_000,
            market_context_batch_fn=lambda _connection, observations: batch_calls.append(observations) or {},
            attribution_fn=lambda *_args: {"stage": "generic"},
            attribution_summary_fn=lambda _rows: {"items": [], "validation_gate": {"status": "accumulating"}},
        )
        # The dashboard asks for a small page, so its attribution projection
        # must never hydrate an unbounded historical board-report window.
        self.assertEqual(connection.execute.call_args_list[0].args[1], (500,))
        self.assertEqual(batch_calls, [[(observed_at, "600000.SH")]])
        self.assertEqual(payload["items"][0]["attribution"]["stage"], "generic")
        self.assertEqual(payload["attribution_window_limit"], 500)
        router = build_intraday_outcome_reads_router(database, lambda *_args: {}, lambda *_args: {}, lambda _rows: {"items": [], "validation_gate": {}})
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/intraday/outcomes/latest"], {"GET"})

    def test_sector_read_model_and_router_bound_member_pages_without_upstream_calls(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        connection.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[])), MagicMock(fetchone=MagicMock(return_value={"total": 0})),
        ]
        page = read_market_sectors(database, "ths_index_n", 10_000, -10)
        self.assertEqual(page["total"], 0)
        self.assertEqual((page["limit"], page["offset"]), (1000, 0))
        self.assertEqual(connection.execute.call_args_list[0].args[1], ("ths_index_n", 1000, 0))
        connection.execute.reset_mock()
        connection.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[])), MagicMock(fetchone=MagicMock(return_value={"total": 0})),
        ]
        members = read_sector_members(database, "885001", "ths_index_n", 10_000, -10)
        self.assertEqual(members["total"], 0)
        self.assertEqual((members["limit"], members["offset"]), (1000, 0))
        self.assertEqual(connection.execute.call_args_list[0].args[1], ("ths_index_n", "885001", 1000, 0))
        router = build_sector_reads_router(database, lambda: True, lambda: 20)
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/market/sectors/concepts/members/backfill/status", "/api/v1/market/sectors/concepts",
            "/api/v1/market/sectors/concepts/candidates", "/api/v1/market/sectors/flows",
            "/api/v1/market/sectors", "/api/v1/market/sectors/{sector_key}/members",
        ):
            self.assertEqual(methods_by_path[path], {"GET"})

    def test_intraday_evidence_read_model_bounds_latest_scan_outbox_rows(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value={"scan_id": "scan-1"})),
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        payload = read_latest_intraday_scan(database, limit=10_000)
        self.assertEqual(payload["scan"]["scan_id"], "scan-1")
        self.assertEqual(connection.execute.call_args_list[1].args[1], ("scan-1", 200))
        self.assertEqual(connection.execute.call_args_list[2].args[1], ("scan-1", 200))
        router = build_intraday_evidence_reads_router(database, lambda _connection, symbol: {"symbol": symbol})
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/intraday/watchlists"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/intraday/decision-cards/{symbol}"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/intraday/scans/latest"], {"GET"})

    def test_market_result_read_model_and_router_keep_results_bounded_and_catalog_checked(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = []
        self.assertEqual(read_market_snapshots(database, 10_000)["items"], [])
        self.assertEqual(connection.execute.call_args.args[1], (100,))
        with self.assertRaises(HTTPException) as caught:
            read_tushare_raw(database, "not_in_catalog", None, 1, 0, {"daily"})
        self.assertEqual(caught.exception.status_code, 404)
        router = build_market_result_reads_router(
            database, {"daily"}, lambda _connection: {}, lambda _connection: {}, lambda: {},
            lambda: Path("/tmp/offline"), lambda _connection: {},
        )
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/providers/tushare/raw", "/api/v1/research/overview", "/api/v1/market/snapshots",
            "/api/v1/market/minute/imports", "/api/v1/analyst-scorecards", "/api/v1/recommendations/latest",
            "/api/v1/metrics",
        ):
            self.assertEqual(methods_by_path[path], {"GET"})

    def test_board_rotation_read_model_and_router_are_bounded_get_only(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = []
        payload = latest_board_rotation_events(database, 1000)
        self.assertEqual(payload["items"], [])
        self.assertIn("LIMIT %s", connection.execute.call_args.args[0])
        self.assertEqual(connection.execute.call_args.args[1], (100,))
        router = build_board_rotation_reads_router(database)
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/intraday/board-rotations/latest"], {"GET"})

    def test_board_curve_read_model_and_router_keep_stored_minute_evidence_bounded(self):
        connection = MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        observed_at = datetime(2026, 8, 10, 1, 21, tzinfo=timezone.utc)
        connection.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[{
                "observed_at": observed_at, "status": "completed",
                "coverage": {"concept": {"flow_boards": 2}},
                "payload": {"items": [
                    {"taxonomy_key": "eastmoney_concept", "sector_key": "BK0917", "label": "芯片", "net_inflow": 3.2, "change_pct": 1.5},
                ]},
                "source": "minute_curve",
            }])),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        payload = read_intraday_board_flow_curves(
            database, date(2026, 8, 10), "concept", None,
            curve_retention_days=60, rotation_retention_days=60,
            now=datetime(2026, 8, 10, 4, tzinfo=timezone.utc),
        )
        self.assertEqual(payload["items"][0]["label"], "芯片")
        self.assertEqual(payload["items"][0]["points"][0]["net_inflow"], 3.2)
        self.assertEqual(payload["cadence_seconds"], 60)
        self.assertIn("LIMIT 720", connection.execute.call_args_list[0].args[0])
        self.assertEqual(len(board_display_slots(date(2026, 8, 10), datetime(2026, 8, 10, 4, tzinfo=timezone.utc))), 131)
        connection.execute.side_effect = [MagicMock(fetchone=MagicMock(return_value=None))]
        self.assertIsNone(read_latest_close_sector_review_report(database)["report"])
        router = build_board_curve_reads_router(database, lambda: 60, lambda: 60)
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/market/sectors/intraday/curves"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/market/sectors/review/report/latest"], {"GET"})

    def test_analyst_reads_router_exposes_text_evidence_as_get_only(self):
        router = build_analyst_reads_router(MagicMock(), lambda _database: {}, lambda _connection, _date, _days: {})
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/remote-archive/state"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/remote-archive/reports"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/remote-archive/messages"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/remote-archive/sync-cursors-global/{stream_key}"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/analyst-claims"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/analyst-factors"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/claim-review"], {"GET"})
        action_router = build_analyst_trade_action_reads_router(MagicMock(), lambda _database, _date, _limit: {})
        self.assertEqual(
            {route.path: route.methods for route in action_router.routes}["/api/v1/analysts/anqiang/trade-actions"],
            {"GET"},
        )
        skill_router = build_analyst_skill_reads_router(MagicMock(), lambda _database, _analyst, _limit: {})
        self.assertEqual({route.path: route.methods for route in skill_router.routes}["/api/v1/analyst-skills"], {"GET"})
        research_router = build_analyst_research_reads_router(MagicMock(), lambda _database, _as_of: {})
        self.assertEqual(
            {route.path: route.methods for route in research_router.routes}["/api/v1/analyst-research/status"], {"GET"},
        )
        self.assertEqual(
            {route.path: route.methods for route in research_router.routes}["/api/v1/analyst-research/profiles"], {"GET"},
        )
        self.assertEqual(
            {route.path: route.methods for route in research_router.routes}["/api/v1/analyst-research/sync-health"], {"GET"},
        )

    def test_analyst_sync_health_distinguishes_never_succeeded_stream(self):
        class _Transaction:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=()):
                if "analyst_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "stream_key": "reports", "remote_analyst_id": "anqiang-touzi-riji",
                        "received_at": None, "message_ids": [], "report_versions": {},
                        "updated_at": datetime.now(timezone.utc),
                    }]))
                if "analyst_global_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                if "workflow_entity" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                return MagicMock(fetchall=MagicMock(return_value=[{
                    "promotion_key": "analyst_delta", "status": "disabled", "max_live_weight": 0,
                }]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        router = build_analyst_research_reads_router(database, lambda _database, _as_of: {})
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")
        payload = asyncio.run(endpoint())
        health = {item["stream_key"]: item for item in payload["stream_health"]}
        self.assertEqual(health["reports"]["status"], "ready")
        self.assertEqual(health["messages"]["status"], "never_succeeded")
        self.assertEqual(payload["runtime_verification"], "pending_next_scheduled_execution")
        self.assertEqual(payload["workflow_health"], [])

    def test_analyst_sync_health_marks_retired_execution_pending_until_current_graph_runs(self):
        class _Transaction:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, _params=()):
                if "analyst_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "stream_key": "reports", "remote_analyst_id": "a",
                        "received_at": None, "message_ids": [], "report_versions": {},
                        "updated_at": datetime.now(timezone.utc),
                    }]))
                if "analyst_global_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "stream_key": "message_updates", "remote_cursor": None,
                        "received_after": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
                    }]))
                if "workflow_entity" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "id": "remoteArchiveReports123", "active": True, "published": True,
                        "latest_execution_status": "error", "latest_started_at": None, "latest_stopped_at": None,
                        "active_version_id": "current", "latest_execution_version_id": "retired",
                    }]))
                return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        router = build_analyst_research_reads_router(database, lambda _database, _as_of: {})
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")
        payload = asyncio.run(endpoint())
        self.assertEqual(payload["workflow_health"][0]["status"], "pending_first_current_execution")
        self.assertEqual(payload["workflow_health"][0]["execution_evidence"], "service_cursor_prior_version")
        self.assertEqual(payload["runtime_verification"], "service_reachable_pending_scheduled_execution")

    def test_analyst_sync_health_requires_success_from_current_published_version(self):
        statements = []

        class _Transaction:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, _params=()):
                statements.append(str(sql))
                if "analyst_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "stream_key": "reports", "remote_analyst_id": "a",
                        "received_at": None, "message_ids": [], "report_versions": {},
                        "updated_at": datetime.now(timezone.utc),
                    }]))
                if "analyst_global_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "stream_key": "message_updates", "remote_cursor": None,
                        "received_after": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
                    }]))
                if "workflow_entity" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[
                        {
                            "id": "remoteArchiveReports123", "active": True, "published": True,
                            "active_version_id": "current", "latest_execution_version_id": "current",
                            "latest_execution_status": "success", "latest_started_at": datetime.now(timezone.utc),
                            "latest_stopped_at": datetime.now(timezone.utc),
                        },
                        {
                            "id": "remoteArchiveMessages123", "active": True, "published": True,
                            "active_version_id": "current", "latest_execution_version_id": "current",
                            "latest_execution_status": "success", "latest_started_at": datetime.now(timezone.utc),
                            "latest_stopped_at": datetime.now(timezone.utc),
                        },
                    ]))
                return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        router = build_analyst_research_reads_router(database, lambda _database, _as_of: {})
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")
        payload = asyncio.run(endpoint())
        self.assertEqual(payload["workflow_health"][0]["status"], "ready")
        self.assertEqual(payload["workflow_health"][0]["execution_evidence"], "current_workflow_execution")
        self.assertEqual(payload["runtime_verification"], "verified_recent_execution")
        expected = {item["stream_key"]: item["expected_workflow_id"] for item in payload["stream_health"]}
        self.assertEqual(expected, {"reports": "remoteArchiveReports123", "messages": "remoteArchiveMessages123"})
        workflow_query = next(statement for statement in statements if "execution_entity" in statement)
        self.assertIn("AND mode='trigger'", workflow_query)

    def test_analyst_sync_health_uses_global_message_cursor(self):
        class _Transaction:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=()):
                if "analyst_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                if "analyst_global_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[{
                        "stream_key": "message_updates", "remote_cursor": None,
                        "received_after": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
                    }]))
                if "workflow_entity" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        router = build_analyst_research_reads_router(database, lambda _database, _as_of: {})
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")
        payload = asyncio.run(endpoint())
        health = {item["stream_key"]: item for item in payload["stream_health"]}
        self.assertEqual(health["messages"]["status"], "ready")
        self.assertEqual(health["messages"]["cursor_count"], 1)

    def test_analyst_sync_health_accepts_recent_zero_item_attempt_without_advancing_cursor(self):
        class _Transaction:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, _params=()):
                if "analyst_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                if "analyst_global_sync_cursors" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                if "analyst_sync_attempts" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[
                        {"stream_key": "reports", "status": "completed", "started_at": datetime.now(timezone.utc),
                         "completed_at": datetime.now(timezone.utc), "error_code": None,
                         "summary": {"items": 0, "source": "remote_text_reports"}},
                        {"stream_key": "messages", "status": "completed", "started_at": datetime.now(timezone.utc),
                         "completed_at": datetime.now(timezone.utc), "error_code": None,
                         "summary": {"items": 0, "source": "remote_text_messages"}},
                    ]))
                if "workflow_entity" in sql:
                    return MagicMock(fetchall=MagicMock(return_value=[]))
                return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        router = build_analyst_research_reads_router(database, lambda _database, _as_of: {})
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/sync-health")
        payload = asyncio.run(endpoint())
        health = {item["stream_key"]: item for item in payload["stream_health"]}
        self.assertEqual(health["reports"]["status"], "ready")
        self.assertEqual(health["messages"]["status"], "ready")
        self.assertEqual(health["messages"]["cursor_count"], 0)
        self.assertEqual(payload["runtime_verification"], "service_reachable_pending_scheduled_execution")

    def test_event_reads_router_keeps_announcements_and_lhb_as_get_only(self):
        router = build_event_reads_router(MagicMock())
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/events/announcements"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/events/lhb"], {"GET"})

    def test_strategy_reads_router_keeps_materialized_results_as_get_only(self):
        router = build_strategy_reads_router(MagicMock(), "test-model")
        methods_by_path = {route.path: route.methods for route in router.routes}
        self.assertEqual(methods_by_path["/api/v1/strategy/decisions/latest"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/strategy/reviews/latest"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/strategy/post-close/latest"], {"GET"})
        self.assertEqual(methods_by_path["/api/v1/strategy/health"], {"GET"})

    def test_strategy_health_is_read_only_and_keeps_validation_gate(self):
        class _Transaction:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=()):
                if "signals_7d" in sql:
                    return MagicMock(fetchone=MagicMock(return_value={
                        "signals_7d": 12, "signals_prior_7d": 4, "episodes_7d": 5,
                        "matured_30m_7d": 8, "matured_days_7d": 3,
                    }))
                if "avg(raw_return)" in sql:
                    return MagicMock(fetchone=MagicMock(return_value={"rows": 8, "positive": 5, "avg_return": 0.003}))
                if "latest_quote_at" in sql:
                    return MagicMock(fetchone=MagicMock(return_value={"latest_quote_at": datetime.now(timezone.utc), "fresh_quote_rows": 2}))
                return MagicMock(fetchall=MagicMock(return_value=[{"strategy_key": "watchlist_confirmation_v4", "signals": 12, "episodes": 5}]))

        database = MagicMock()
        database.transaction.return_value = _Transaction()
        payload = latest_strategy_health(database, now=datetime.now(timezone.utc))
        self.assertEqual(payload["status"], "research_only")
        self.assertEqual(payload["trigger_frequency"]["drift_status"], "stable")
        self.assertEqual(payload["validation_gate"]["status"], "accumulating")
        self.assertEqual(payload["validation_gate"]["live_effect"], "none")
        self.assertEqual(payload["governance_recommendation"]["action"], "keep_descriptive_only")
        self.assertEqual(payload["governance_recommendation"]["live_effect"], "none")

    def test_strategy_health_recommendation_freezes_only_on_stale_quotes(self):
        stale = health_recommendation(
            drift_status="stable", quote_status="stale_or_missing",
            gate_status="ready_for_formal_validation", matured=200, trading_days=60,
        )
        self.assertEqual(stale["action"], "freeze_new_entries")
        self.assertEqual(stale["live_effect"], "none")
        review = health_recommendation(
            drift_status="warning", quote_status="fresh",
            gate_status="accumulating", matured=8, trading_days=3,
        )
        self.assertEqual(review["action"], "manual_review")

    def test_strategy_health_rolls_symbol_signal_keys_up_to_strategy_families(self):
        rows = [
            {"strategy_key": "000001.SZ:watch:extreme_flow_buy", "signals": 5, "episode_ids": ["a", "b"]},
            {"strategy_key": "000002.SZ:reduce:extreme_flow_sell", "signals": 4, "episode_ids": ["c"]},
            {"strategy_key": "000003.SZ:entry:watchlist-confirmation-v4", "signals": 3, "episode_ids": ["d"]},
        ]
        breakdown = strategy_family_breakdown(rows)
        self.assertEqual(breakdown[0], {
            "strategy_key": "extreme_flow", "strategy_family": "extreme_flow", "signals": 9, "episodes": 3,
        })
        self.assertEqual(breakdown[1]["strategy_key"], "watchlist-confirmation-v4")

    def test_intraday_alert_text_keeps_strategy_evidence_and_disclaimer(self):
        text = intraday_alert_text(
            {"symbol": "600000.SH", "signal_type": "watch", "conditions": {
                "setup": "eac_first_intraday_high", "price": 12.3, "pct_change": 2.1,
                "volume_ratio": 3.2, "turnover_rate": 4.5, "main_net_inflow": 100,
                "eac_state": "attention_only", "upside_research_assessment": {"metrics": {
                    "return_3m_pct": 1.2, "minute_volume_multiple": 4.3, "above_vwap_pct": 0.8,
                    "session_window": "09:40-10:45", "time_bucket_volume_profile": {"status": "ready", "sample_days": 20},
                    "time_bucket_volume_surprise": 2.5,
                }},
                "realtime_cross_check": {"status": "confirmed", "super_get_price": 12.3, "tencent_price": 12.29, "gap_pct": 0.08},
                "decision_context": {
                    "action": "入场复核", "reasons": ["首突破后量价同步"],
                    "invalidations": ["跌回VWAP"],
                    "probability": {"estimated_probability": None, "historical_condition_baseline": 0.54, "raw_positive_rate": 0.6,
                                    "sample_rows": 25, "independent_trading_days": 3,
                                    "average_directional_return": 0.002, "horizon": "30m",
                                    "confidence_tier": "uncalibrated"},
                },
            }},
            {"label": "浦发银行"}, {"name": "浦发银行"}, {"time": "2026-08-11 10:00:00", "close": 12.3},
            decision_card_url="https://research.example/?section=research&tab=stock-study&symbol=600000.SH",
        )
        self.assertIn("EAC 首突破", text)
        self.assertIn("秒级价格交叉确认", text)
        self.assertIn("信号观测时间（上海）", text)
        self.assertIn("决策卡（已保存证据）", text)
        self.assertIn("建议方向：入场复核", text)
        self.assertIn("触发原因1：首突破后量价同步", text)
        self.assertIn("研究概率：暂不可估", text)
        self.assertIn("历史条件基准 54.0%（未校准，不作为概率或仓位依据）", text)
        self.assertIn("匹配成熟样本 25", text)
        self.assertIn("失效/反证条件：跌回VWAP", text)
        self.assertIn("仅为人工复核提醒", text)

    def test_intraday_alert_text_never_turns_score_into_probability(self):
        text = intraday_alert_text(
            {"symbol": "600000.SH", "signal_type": "exit", "score": 100, "conditions": {
                "price": 9.4, "pct_change": -4, "volume_ratio": 2, "turnover_rate": 3,
                "main_net_inflow": -100, "decision_context": {
                    "action": "离场复核", "reasons": ["触发硬止损"],
                    "invalidations": ["核对可卖数量"],
                    "probability": {"estimated_probability": None, "sample_rows": 0},
                },
            }}, {"label": "样本"}, {"name": "样本"}, None,
        )
        self.assertIn("研究概率：暂不可估", text)
        self.assertIn("策略评分不是概率", text)
        self.assertNotIn("100.0%", text)

    def test_daily_strategy_summary_keeps_data_gate_and_avoids_small_sample_win_rate(self):
        text = daily_strategy_summary_text({
            "exchange_date": "2026-08-11", "signal_counts": {"alerted": 2, "confirmed": 1, "suppressed": 3},
            "outcome_counts": {"5m": {"matured": 1, "pending": 2}},
            "post_close": {"status": "blocked", "reason": "daily coverage is incomplete", "candidates": []},
            "readiness": {"decision_ready": False, "blockers": ["daily_basic", "trade_limits"]},
            "offline_policy_learning": {
                "validation_gate": {"status": "accumulating", "matured_unique_signals": 1, "trading_days": 1,
                                    "required_unique_signals": 200, "required_trading_days": 60},
                "daily_review": {"delivered_signals": 2, "matured_30m_signals": 1},
            },
        }, "https://research.example")
        self.assertIn("日终研究摘要", text)
        self.assertIn("盘后候选：blocked", text)
        self.assertIn("daily_basic、trade_limits", text)
        self.assertIn("策略学习", text)
        self.assertIn("未自动改参", text)
        self.assertIn("不展示胜率", text)

    def test_delivery_health_recovery_receipt_is_operational_not_a_market_signal(self):
        text = delivery_health_recovery_text(3)
        self.assertIn("连续 3 次投递失败", text)
        self.assertIn("本地 outbox", text)
        self.assertIn("不构成交易或市场判断", text)

    def test_feishu_alert_delivery_is_disabled_without_opt_in_configuration(self):
        with patch.dict("os.environ", {"QUANT_ALERT_WEBHOOK_URL": "", "QUANT_ALERT_WEBHOOK_TOKEN": ""}):
            result = asyncio.run(post_feishu_alert_text("test"))
        self.assertEqual(result["status"], "disabled")

    def test_exchange_date_and_limit_ratio_use_cn_market_rules(self):
        self.assertEqual(cn_today(datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)), date(2026, 8, 11))
        self.assertEqual(cn_today(datetime(2026, 8, 10, 23, 30, tzinfo=timezone.utc)), date(2026, 8, 11))
        self.assertEqual(a_share_limit_ratio("600000.SH"), 0.10)
        self.assertEqual(a_share_limit_ratio("300750.SZ"), 0.20)
        self.assertEqual(a_share_limit_ratio("688001.SH"), 0.20)
        self.assertEqual(a_share_limit_ratio("830001.BJ"), 0.30)
        self.assertEqual(a_share_limit_ratio("600000.SH", True), 0.05)
        self.assertTrue(is_st_security_name("*ST美丽"))
        self.assertTrue(is_st_security_name("ST海王"))
        self.assertFalse(is_st_security_name("东方财富"))

    def test_cninfo_announcement_transport_is_https_only(self):
        from app import free_market_providers
        source = Path(free_market_providers.__file__).read_text()
        self.assertIn('https://www.cninfo.com.cn/new/hisAnnouncement/query', source)
        self.assertIn('https://static.cninfo.com.cn/', source)
        self.assertNotIn('http://www.cninfo.com.cn/new/hisAnnouncement/query', source)

    def test_write_access_requires_the_dedicated_key_when_configured(self):
        self.assertTrue(write_access_allowed("GET", None, "configured"))
        self.assertTrue(write_access_allowed("POST", None, ""))
        self.assertFalse(write_access_allowed("POST", None, "configured"))
        self.assertFalse(write_access_allowed("DELETE", "wrong", "configured"))
        self.assertTrue(write_access_allowed("PATCH", "configured", "configured"))

    def test_remote_archive_sync_accepts_only_a_bearer_shaped_trigger(self):
        from starlette.requests import Request

        def request(path: str, authorization: str | None) -> Request:
            headers = [] if authorization is None else [(b"authorization", authorization.encode())]
            return Request({"type": "http", "method": "POST", "path": path, "headers": headers})

        self.assertTrue(remote_archive_sync_bearer_allowed(request("/api/v1/remote-archive/sync", "Bearer " + "a" * 32)))
        self.assertFalse(remote_archive_sync_bearer_allowed(request("/api/v1/remote-archive/sync", None)))
        self.assertFalse(remote_archive_sync_bearer_allowed(request("/api/v1/remote-archive/sync", "Bearer too-short")))
        self.assertFalse(remote_archive_sync_bearer_allowed(request("/api/v1/remote-archive/messages/import", "Bearer " + "a" * 32)))

    def test_remote_archive_get_honors_bounded_retry_after_for_429(self):
        responses = [
            httpx.Response(429, headers={"Retry-After": "1"}, text='{"error":"rate_limited"}'),
            httpx.Response(503, text='{"error":"busy"}'),
            httpx.Response(200, json={"items": []}),
        ]
        client = MagicMock()
        client.get = AsyncMock(side_effect=responses)
        settings = {"request_interval_seconds": 0.0}
        result = asyncio.run(
            remote_archive_get(client, "/messages/updates", settings=lambda: settings, sleep=AsyncMock())
        )
        self.assertEqual(result, {"items": []})
        self.assertEqual(client.get.await_count, 3)

    def test_provider_failure_recording_redacts_credentials(self):
        connection = MagicMock()
        record_provider_failure(connection, "test", "daily", "Authorization: credential-value", latency_ms=123)
        parameters = connection.execute.call_args.args[1]
        self.assertNotIn("credential-value", parameters[-2])
        self.assertEqual(parameters[-1], 123)
        self.assertIn("last_latency_ms", connection.execute.call_args.args[0])

    def test_provider_success_keeps_last_latency_when_sample_has_no_latency(self):
        connection = MagicMock()
        record_provider_success(connection, "test", "daily", 1)
        sql = connection.execute.call_args.args[0]
        self.assertIn("COALESCE(EXCLUDED.last_latency_ms", sql)
        self.assertIn("quant.provider_health.last_latency_ms", sql)

    def test_provider_failure_keeps_last_latency_when_sample_has_no_latency(self):
        connection = MagicMock()
        record_provider_failure(connection, "test", "daily", "temporary")
        sql = connection.execute.call_args.args[0]
        self.assertIn("COALESCE(EXCLUDED.last_latency_ms", sql)

    def test_intraday_outcome_decomposition_is_json_safe_before_persistence(self):
        from app.main import strategy_json_safe
        decomposition = a_share_return_decomposition(
            Decimal("10"), 1, Decimal("10.5"), Decimal("10.2"), Decimal("10.8"),
        )
        persisted = strategy_json_safe({"return_decomposition": decomposition})
        self.assertIsInstance(persisted["return_decomposition"]["overnight"], str)

    def test_capability_circuit_lookup_returns_only_open_entries(self):
        async def check() -> set[str]:
            with patch("app.main.read_async_open_provider_capabilities", new=AsyncMock(return_value={"intraday_board_flow_concept"})):
                return await open_provider_capabilities(
                    "eastmoney_free", ["intraday_board_flow_concept", "intraday_board_flow_industry"],
                )
        self.assertEqual(asyncio.run(check()), {"intraday_board_flow_concept"})

    def test_generic_provider_circuit_lookup_uses_native_async_repository(self):
        providers = [MagicMock(key="tushare_primary"), MagicMock(key="tushare_super_sdk")]

        async def check() -> set[str]:
            with patch("app.main.read_async_open_provider_keys", new=AsyncMock(return_value={"tushare_super_sdk"})):
                return await circuit_open_provider_keys_async("daily", providers)

        self.assertEqual(asyncio.run(check()), {"tushare_super_sdk"})


class DailyPipelineTakesTheCrossSectionInOneRequestTests(unittest.TestCase):
    """The market stages must not iterate the universe symbol by symbol.

    The per-symbol synchronizers this replaced issued one provider call per
    name across ~5.5k symbols. Measured on 2026-08-27 they imported about 1.8
    bars a minute, so the request died at 851s having loaded 41 of 5547 bars
    and nothing downstream of the sync - settlement included - ever ran.
    """

    def _run(self, *, primary_status="completed"):
        seen = {}

        async def sync_full_market_daily(request):
            seen["request"] = request
            return {"status": primary_status}

        async def sync_baostock(_request):
            seen["baostock"] = True
            return {"status": "completed"}

        controls = AsyncMock(return_value={"status": "completed"})

        async def blocking(operation, *_args, **_kwargs):
            return {"status": "ready"} if operation is build else {}

        build = object()
        asyncio.run(run_pipeline(
            GenerateRequest(as_of_date=date(2026, 8, 27)),
            sync_full_market_daily=sync_full_market_daily, sync_baostock=sync_baostock,
            sync_full_market_daily_controls=controls,
            tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
            snapshot_request=lambda as_of: {"as_of_date": as_of}, build_snapshot=build,
            recompute_outcomes=object(), recompute_scorecards=object(),
            generate_recommendations=object(), run_database_blocking=blocking,
            cn_today=lambda: date(2026, 8, 27),
        ))
        return seen, controls

    def test_the_daily_stage_asks_for_one_whole_trade_date(self):
        seen, _ = self._run()
        self.assertEqual(seen["request"], {"trade_date": date(2026, 8, 27)},
                         "the stage must request a trade date, never a single symbol")

    def test_controls_run_on_the_resolved_date_not_none(self):
        _, controls = self._run()
        controls.assert_awaited_once_with(date(2026, 8, 27))

    def test_a_blocked_primary_falls_back_rather_than_settling_on_a_partial_date(self):
        # The full-market synchronizer reports "blocked", which the per-symbol
        # one never did; without it in the set the fallback silently stopped.
        seen, controls = self._run(primary_status="blocked")
        self.assertTrue(seen.get("baostock"), "a blocked cross-section must reach the fallback")
        controls.assert_not_awaited()



class MinuteBarBackfillRunsLastAndNeverFailsThePipelineTests(unittest.TestCase):
    """The minute-bar pass is best-effort supplementary data.

    stk_mins answered ~55% of sampled boards over three closed sessions and 0%
    intraday, so it is wired to run after every decision output already exists,
    with a bounded budget and a failure that is reported rather than fatal.
    """

    def _pipeline(self, backfill, *, budget=None, order=None):
        async def tracked(operation, *_a, **_k):
            if order is not None:
                order.append("db")
            return {"status": "ready"}

        async def wrapped(as_of):
            if order is not None:
                order.append("backfill")
            return await backfill(as_of)

        kwargs = dict(
            sync_full_market_daily=AsyncMock(return_value={"status": "completed"}),
            sync_baostock=AsyncMock(return_value={"status": "completed"}),
            sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
            tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
            snapshot_request=lambda a: {"as_of_date": a}, build_snapshot=object(),
            recompute_outcomes=object(), recompute_scorecards=object(),
            generate_recommendations=object(), run_database_blocking=tracked,
            cn_today=lambda: date(2026, 8, 28), backfill_minute_bars=wrapped)

        def go():
            return asyncio.run(run_pipeline(GenerateRequest(as_of_date=date(2026, 8, 28)), **kwargs))

        if budget is not None:
            with patch("app.daily_pipeline.MINUTE_BACKFILL_BUDGET_SECONDS", budget):
                return go()
        return go()

    def test_the_backfill_runs_after_the_decision_stages(self):
        order = []

        async def ok(_a):
            return {"availability_pct": 55.0, "bars": 2885}

        result = self._pipeline(ok, order=order)
        self.assertEqual(order[-1], "backfill")
        self.assertEqual(result["minute_bars"], {"availability_pct": 55.0, "bars": 2885})

    def test_a_failing_backfill_leaves_the_pipeline_completed(self):
        async def boom(_a):
            raise RuntimeError("stk_mins 202")

        result = self._pipeline(boom)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["minute_bars"]["status"], "failed")
        self.assertIn("RuntimeError", result["minute_bars"]["error"])

    def test_a_slow_backfill_is_bounded_by_the_budget(self):
        async def hang(_a):
            await asyncio.sleep(1.0)

        result = self._pipeline(hang, budget=0.05)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["minute_bars"]["status"], "timeout")
        self.assertEqual(result["minute_bars"]["budget_seconds"], 0.05)

    def test_the_pipeline_completes_unchanged_without_a_backfill_wired(self):
        result = asyncio.run(run_pipeline(
            GenerateRequest(as_of_date=date(2026, 8, 28)),
            sync_full_market_daily=AsyncMock(return_value={"status": "completed"}),
            sync_baostock=AsyncMock(return_value={"status": "completed"}),
            sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
            tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
            snapshot_request=lambda a: {"as_of_date": a}, build_snapshot=object(),
            recompute_outcomes=object(), recompute_scorecards=object(),
            generate_recommendations=object(),
            run_database_blocking=AsyncMock(return_value={"status": "ready"}),
            cn_today=lambda: date(2026, 8, 28)))
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["minute_bars"])

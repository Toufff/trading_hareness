"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403
from app.tushare_providers import PROMAX_VERIFIED_APIS


class ProviderAndRealtimeRuleTests(unittest.TestCase):
    def test_runtime_resource_thresholds_are_bounded_and_explain_degradation(self):
        self.assertEqual(bounded_min_free_bytes("invalid"), 1024 ** 3)
        self.assertEqual(bounded_warning_free_bytes("invalid", 8 * 1024 ** 3), 10 * 1024 ** 3)
        self.assertEqual(bounded_memory_ratio("2"), 0.98)
        self.assertEqual(
            bounded_storage_budget_bytes(
                str(200 * 1024 ** 3), DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
                DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
            ),
            DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
        )
        self.assertEqual(
            bounded_storage_budget_bytes(
                str(40 * 1024 ** 3), DEFAULT_HOT_DATABASE_SOFT_BYTES,
                DEFAULT_HOT_DATABASE_SOFT_BYTES,
            ),
            DEFAULT_HOT_DATABASE_SOFT_BYTES,
        )
        state, reasons = runtime_resource_state(
            disk_free_bytes=10, min_free_bytes=100, rss_bytes=90, memory_limit_bytes=100, max_memory_ratio=0.85,
        )
        self.assertEqual(state, "degraded")
        self.assertEqual(len(reasons), 2)

    def test_runtime_resource_warns_before_the_disk_stop_floor(self):
        state, reasons = runtime_resource_state(
            disk_free_bytes=9 * 1024 ** 3,
            min_free_bytes=8 * 1024 ** 3,
            warning_free_bytes=10 * 1024 ** 3,
            rss_bytes=10,
            memory_limit_bytes=100,
            max_memory_ratio=0.85,
        )
        self.assertEqual(state, "warning")
        self.assertEqual(reasons, ["persistent storage free space is below the configured warning watermark"])

    def test_research_storage_governance_warns_then_stops_only_nonessential_capture(self):
        healthy = research_storage_governance(
            hot_database_bytes=1, artifact_bytes=1,
            research_budget_bytes=DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
            hot_database_budget_bytes=DEFAULT_HOT_DATABASE_SOFT_BYTES,
            warning_ratio=0.8, stop_ratio=0.9,
        )
        self.assertEqual(healthy["state"], "healthy")
        self.assertTrue(healthy["allow_nonessential_high_frequency"])
        warning = research_storage_governance(
            hot_database_bytes=int(DEFAULT_HOT_DATABASE_SOFT_BYTES * 0.85), artifact_bytes=0,
            research_budget_bytes=DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
            hot_database_budget_bytes=DEFAULT_HOT_DATABASE_SOFT_BYTES,
            warning_ratio=0.8, stop_ratio=0.9,
        )
        self.assertEqual(warning["state"], "warning")
        self.assertTrue(warning["allow_nonessential_high_frequency"])
        stopped = research_storage_governance(
            hot_database_bytes=int(DEFAULT_HOT_DATABASE_SOFT_BYTES * 0.95), artifact_bytes=0,
            research_budget_bytes=DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
            hot_database_budget_bytes=DEFAULT_HOT_DATABASE_SOFT_BYTES,
            warning_ratio=0.8, stop_ratio=0.9,
        )
        self.assertEqual(stopped["state"], "stop_nonessential_high_frequency")
        self.assertFalse(stopped["allow_nonessential_high_frequency"])

    def test_provider_catalog_snapshot_keeps_get_and_sdk_observations_separate(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"provider_key": "tushare_super_get", "api_name": "daily", "availability": "verified",
             "verified_at": None, "last_checked_at": None, "metadata": {"last_row_count": 2}},
            {"provider_key": "tushare_super_sdk", "api_name": "adj_factor", "availability": "verified",
             "verified_at": None, "last_checked_at": None, "metadata": {}},
        ]
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        snapshot = tushare_catalog_snapshot(
            database,
            catalog_items_fn=lambda: [{"api_name": "daily"}, {"api_name": "adj_factor"}],
            catalog_counts_fn=lambda: {"declared": 2}, provider_status_fn=lambda: [], free_provider_status_fn=lambda: [],
        )
        daily, adj_factor = snapshot["items"]
        self.assertEqual(daily["super_get_availability"], "verified")
        self.assertEqual(daily["super_availability"], "verified")
        self.assertEqual(adj_factor["super_sdk_availability"], "verified")
        self.assertEqual(adj_factor["super_availability"], "verified")

    def test_stock_study_fetch_reads_tushare_evidence_in_database_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=[])
            outcome = {"request_key": "request-1", "provider": "super", "status": "completed", "received": 0, "stored": 0}
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(return_value=outcome)), \
                 patch("app.main.run_database_blocking", new=blocking):
                source, _ = await stock_study_fetch("daily", TushareFetchRequest(api_name="daily", params={"ts_code": "000001.SZ"}))
            return source, blocking

        source, blocking = asyncio.run(check())
        self.assertEqual(source["status"], "completed")
        self.assertEqual(blocking.await_args.args[0].__name__, "tushare_rows_for_request")

    def test_tushare_fetch_prepares_or_reuses_ledger_in_database_executor(self):
        provider = MagicMock(key="tushare_super_sdk")
        cached = {"status": "unchanged", "api_name": "daily", "request_key": "cached", "provider": provider.key,
                  "stored": 1, "normalized_rows": 1, "complete": True}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=cached)
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.circuit_open_provider_keys_async", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.call_tushare_api", new=AsyncMock()) as upstream:
                result = await fetch_tushare_catalog(TushareFetchRequest(api_name="daily", provider="super", params={"ts_code": "000001.SZ"}))
            upstream.assert_not_awaited()
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, cached)
        self.assertEqual(blocking.await_args.args[0].__name__, "prepare_tushare_fetch_run")

    def test_tushare_fetch_success_persists_its_atomic_evidence_transaction_in_database_executor(self):
        provider = MagicMock(key="tushare_super_sdk")
        result = MagicMock(rows=[{"ts_code": "000001.SZ", "trade_date": "20260811", "close": 10}], complete=True,
                           provider=provider, failed_providers=(), empty_providers=(), pages=1)

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[None, ("completed", 1)])
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.circuit_open_provider_keys_async", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.call_tushare_api", new=AsyncMock(return_value=result)):
                value = await fetch_tushare_catalog(TushareFetchRequest(api_name="daily", provider="super", params={"ts_code": "000001.SZ"}))
            return value, blocking

        value, blocking = asyncio.run(check())
        self.assertEqual(value["status"], "completed")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "prepare_tushare_fetch_run", "persist_tushare_fetch_success",
        ])
        self.assertIsInstance(blocking.await_args_list[-1].args[-1], int)
        self.assertGreaterEqual(blocking.await_args_list[-1].args[-1], 0)

    def test_tushare_fetch_failure_marks_the_ledger_in_database_executor(self):
        provider = MagicMock(key="tushare_super_sdk")

        async def check() -> AsyncMock:
            blocking = AsyncMock(side_effect=[None, None])
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.circuit_open_provider_keys_async", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.call_tushare_api", new=AsyncMock(side_effect=ProviderCallError("upstream failed"))):
                with self.assertRaises(HTTPException) as caught:
                    await fetch_tushare_catalog(TushareFetchRequest(api_name="daily", provider="super", params={"ts_code": "000001.SZ"}))
            self.assertEqual(caught.exception.status_code, 502)
            return blocking

        blocking = asyncio.run(check())
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "prepare_tushare_fetch_run", "persist_tushare_fetch_failure",
        ])

    def test_tushare_fetch_local_capacity_marks_blocked_without_provider_failure(self):
        provider = MagicMock(key="tushare_super_get")

        async def check() -> AsyncMock:
            blocking = AsyncMock(side_effect=[None, None])
            with patch("app.main.provider_candidates", return_value=[provider]), \
                 patch("app.main.circuit_open_provider_keys_async", new=AsyncMock(return_value=set())), \
                 patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.call_tushare_api", new=AsyncMock(side_effect=ExecutorSaturatedError("super_get blocking executor is saturated"))):
                with self.assertRaises(HTTPException) as caught:
                    await fetch_tushare_catalog(TushareFetchRequest(api_name="daily", provider="super", params={"ts_code": "000001.SZ"}))
            self.assertEqual(caught.exception.status_code, 503)
            return blocking

        blocking = asyncio.run(check())
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], [
            "prepare_tushare_fetch_run", "persist_tushare_fetch_blocked",
        ])

    def test_tushare_capability_audit_keeps_local_capacity_distinct_from_provider_failure(self):
        async def check() -> dict[str, object]:
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(side_effect=HTTPException(
                status_code=503, detail="local processing capacity is temporarily saturated; retry shortly",
            ))):
                return await audit_tushare_capabilities(TushareCapabilityAuditRequest(
                    api_names=["daily"], providers=["super"], symbol="000001.SZ",
                ))

        result = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["results"][0]["status"], "blocked")
        self.assertEqual(result["results"][0]["availability"], "local_capacity")

    def test_local_capacity_and_circuit_open_http_errors_have_distinct_states(self):
        local = HTTPException(status_code=503, detail="local processing capacity is temporarily saturated; retry shortly")
        circuit = HTTPException(status_code=503, detail="all configured providers are temporarily circuit-open for daily")
        self.assertTrue(is_local_capacity_http_error(local))
        self.assertFalse(is_circuit_open_http_error(local))
        self.assertFalse(is_local_capacity_http_error(circuit))
        self.assertTrue(is_circuit_open_http_error(circuit))

    def test_stock_study_fetch_preserves_local_capacity_and_circuit_open_states(self):
        async def check() -> tuple[dict[str, object], dict[str, object]]:
            local = HTTPException(status_code=503, detail="local processing capacity is temporarily saturated; retry shortly")
            circuit = HTTPException(status_code=503, detail="all configured providers are temporarily circuit-open for daily")
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock(side_effect=[local, circuit])):
                request = TushareFetchRequest(api_name="daily", params={"ts_code": "000001.SZ"})
                first, _ = await stock_study_fetch("daily", request)
                second, _ = await stock_study_fetch("daily", request)
            return first, second

        local, circuit = asyncio.run(check())
        self.assertEqual(local["status"], "blocked")
        self.assertEqual(circuit["status"], "circuit_open")

    def test_tushare_caller_cancellation_is_blocked_without_provider_health_penalty(self):
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        with patch("app.main.db.transaction", return_value=context), \
             patch("app.main.record_provider_failure") as failure, \
             patch("app.main.record_provider_api_capability") as capability:
            persist_tushare_fetch_cancel("request-key", "daily", ["tushare_primary", "tushare_super_get"])
        params = connection.execute.call_args.args[1]
        self.assertEqual(params, ("request-key",))
        self.assertIn("status='blocked'", connection.execute.call_args.args[0])
        self.assertIn("caller_cancelled", connection.execute.call_args.args[0])
        failure.assert_not_called()
        capability.assert_not_called()

    def test_stock_study_timeout_is_reported_as_local_blocking_not_provider_failure(self):
        async def timeout_without_leaking(awaitable: object, timeout: float) -> object:
            # ``asyncio.wait_for`` closes/cancels its child task on timeout.
            # Our replacement must do the same, otherwise the mocked fetch
            # coroutine is left unawaited and masks real async resource leaks.
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError

        async def check() -> dict[str, object]:
            with patch("app.main.fetch_tushare_catalog", new=AsyncMock()), \
                 patch("app.main.asyncio.wait_for", new=timeout_without_leaking):
                source, _ = await stock_study_fetch("daily", TushareFetchRequest(api_name="daily", params={"ts_code": "000001.SZ"}))
            return source

        source = asyncio.run(check())
        self.assertEqual(source["status"], "blocked")
        self.assertIn("local budget", str(source["error"]))

    def test_blocked_strategy_decision_persists_through_database_executor(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=None)
            with patch("app.main.intraday_sector_report", new=AsyncMock(return_value={"status": "blocked", "reason": "closed"})), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await run_strategy_decision(StrategyDecisionRequest(session="close", validate_tushare_realtime=False))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(blocking.await_args.args[0].__name__, "persist_blocked")

    def test_completed_strategy_decision_offloads_all_repository_context_reads(self):
        async def check() -> tuple[dict[str, object], list[str]]:
            calls: list[str] = []

            async def blocking(operation, *args, **kwargs):
                calls.append(operation.__name__)
                if operation.__name__ in {"strategy_event_context", "strategy_tushare_lhb_context"}:
                    return {}
                if operation.__name__ == "strategy_source_readiness":
                    return {"providers": {}, "post_close_event_inventory": []}
                self.assertEqual(operation.__name__, "persist_completed")
                return None

            report = {"status": "completed", "items": [], "coverage": {}, "tushare_context": {}}
            with patch("app.main.intraday_sector_report", new=AsyncMock(return_value=report)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await run_strategy_decision(StrategyDecisionRequest(session="close", validate_tushare_realtime=False))
            return result, calls

        result, calls = asyncio.run(check())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(calls), {
            "strategy_event_context", "strategy_tushare_lhb_context", "strategy_source_readiness", "persist_completed",
        })

    def test_database_pool_settings_are_bounded(self):
        self.assertEqual(pool_settings({"QUANT_DB_POOL_MIN_SIZE": "0", "QUANT_DB_POOL_MAX_SIZE": "999"}),
                         {"min_size": 1, "max_size": 32, "timeout_seconds": 10})
        self.assertEqual(pool_settings({"QUANT_DB_POOL_MIN_SIZE": "4", "QUANT_DB_POOL_MAX_SIZE": "3", "QUANT_DB_POOL_TIMEOUT_SECONDS": "2"}),
                         {"min_size": 4, "max_size": 4, "timeout_seconds": 2})

    def test_akshare_retry_is_bounded_and_returns_the_first_success(self):
        with patch("app.akshare_provider._call", side_effect=[AkShareProviderError("temporary disconnect"), [{"code": "000001"}]] ) as call, \
             patch("app.akshare_provider.time.sleep") as sleep:
            rows = _retry_call("test", lambda _ak: None, attempts=2)
        self.assertEqual(rows, [{"code": "000001"}])
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(0.35)

    def test_akshare_default_retry_is_one_retry_not_three_attempts(self):
        with patch("app.akshare_provider._call", side_effect=[AkShareProviderError("temporary disconnect"), AkShareProviderError("still unavailable")]) as call, \
             patch("app.akshare_provider.time.sleep") as sleep:
            with self.assertRaises(AkShareProviderError):
                _retry_call("test", lambda _ak: None)
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(0.35)

    def test_public_http_retry_only_retries_a_transient_server_failure_once(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            headers = {"Retry-After": "2"} if calls == 1 else {}
            return httpx.Response(503 if calls == 1 else 200, headers=headers, request=request)

        async def check() -> int:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch("app.free_market_providers.asyncio.sleep", new=AsyncMock()) as sleep:
                    response = await _request_with_retry(client, "GET", "https://example.test/quote")
                sleep.assert_awaited_once_with(2.0)
                return response.status_code

        self.assertEqual(asyncio.run(check()), 200)
        self.assertEqual(calls, 2)

    def test_public_daily_persistence_uses_one_transaction_after_validation(self):
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        with patch("app.main.db.transaction", return_value=context) as transaction, \
             patch("app.main.upsert_bar") as upsert:
            stored = persist_free_daily("akshare", [
                {"ts_code": "600000.SH", "trade_date": "20260810", "open": 10, "high": 11, "low": 9, "close": 10.5},
                {"ts_code": "not-a-symbol", "trade_date": "20260810", "close": 10},
                {"ts_code": "000001.SZ", "trade_date": "20260810", "open": 8, "high": 9, "low": 7, "close": 8.5},
            ])
        self.assertEqual(stored, 2)
        transaction.assert_called_once()
        self.assertEqual(upsert.call_count, 2)

    def test_tencent_front_adjusted_daily_rows_remain_raw_research_evidence(self):
        rows = [{"ts_code": "600000.SH", "trade_date": "20260810", "open": 10, "high": 11, "low": 9, "close": 10.5}]
        with patch("app.main.persist_public_observations", return_value=1) as raw_only, \
             patch("app.main.upsert_bar") as canonical:
            stored = persist_free_daily("tencent_free", rows)
        self.assertEqual(stored, 1)
        raw_only.assert_called_once_with("tencent_free", "daily_bar", rows)
        canonical.assert_not_called()

    def test_tencent_front_adjusted_rows_are_rejected_by_canonical_upsert(self):
        connection = MagicMock()
        with self.assertRaisesRegex(ValueError, "front-adjusted"):
            from app.main import upsert_bar
            upsert_bar(connection, DailyBar(
                symbol="600000.SH", trading_date=date(2026, 8, 10), close=Decimal("10"), source="tencent_free",
            ))
        connection.execute.assert_not_called()

    def test_public_market_repository_has_no_router_or_provider_dependency(self):
        source = Path("app/public_market_repository.py").read_text(encoding="utf-8")
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertNotIn("requests", source)
        self.assertIn("raw_market_observations", source)

    def test_http_transport_ownership_stays_in_lifecycle_or_provider_adapters(self):
        app_dir = Path("app")
        importers = {
            path.name for path in app_dir.glob("*.py")
            if "import httpx" in path.read_text(encoding="utf-8")
            or "from httpx" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(importers, {
            "alert_transport.py", "free_market_providers.py", "http_clients.py",
            "feishu_direct_alert.py", "fuyao_provider.py", "main.py", "remote_archive_sync.py",
            "remote_archive_transport.py", "tushare_providers.py",
        })
        self.assertNotIn("AsyncClient(", (app_dir / "free_market_providers.py").read_text(encoding="utf-8"))
        self.assertIn("public_http_client()", (app_dir / "free_market_providers.py").read_text(encoding="utf-8"))
        self.assertNotIn("AsyncClient(", (app_dir / "tushare_providers.py").read_text(encoding="utf-8"))
        self.assertIn("provider_http_client(", (app_dir / "tushare_providers.py").read_text(encoding="utf-8"))
        self.assertNotIn("AsyncClient(", (app_dir / "fuyao_provider.py").read_text(encoding="utf-8"))
        self.assertIn("provider_http_client(", (app_dir / "fuyao_provider.py").read_text(encoding="utf-8"))
        self.assertIn("alert_http_client()", (app_dir / "alert_transport.py").read_text(encoding="utf-8"))
        self.assertIn("alert_http_client", (app_dir / "feishu_direct_alert.py").read_text(encoding="utf-8"))

    def test_legacy_schema_bootstrap_is_explicit_opt_in(self):
        self.assertFalse(legacy_schema_bootstrap_enabled({}))
        self.assertFalse(legacy_schema_bootstrap_enabled({"QUANT_LEGACY_SCHEMA_BOOTSTRAP": "false"}))
        self.assertTrue(legacy_schema_bootstrap_enabled({"QUANT_LEGACY_SCHEMA_BOOTSTRAP": "yes"}))

    def test_normalization_promotes_st_suspension_adjustment_and_limits(self):
        class RecordingConnection:
            def __init__(self): self.calls = []
            def execute(self, sql, params=None):
                self.calls.append((" ".join(sql.split()), params))
                return MagicMock()

        connection = RecordingConnection()
        observed = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(normalize_tushare_rows(connection, "stock_basic", [{"ts_code": "600001.SH", "name": "*ST示例"}], observed), 1)
        self.assertEqual(normalize_tushare_rows(connection, "suspend_d", [{"ts_code": "600001.SH", "trade_date": "20260810", "resume_date": "20260812"}], observed), 1)
        self.assertEqual(normalize_tushare_rows(connection, "adj_factor", [{"ts_code": "600001.SH", "trade_date": "20260810", "adj_factor": "1.25"}], observed), 1)
        self.assertEqual(normalize_tushare_rows(connection, "stk_limit", [{"ts_code": "600001.SH", "trade_date": "20260810", "up_limit": "11", "down_limit": "9"}], observed), 1)
        sql = "\n".join(statement for statement, _ in connection.calls)
        self.assertIn("is_st=EXCLUDED.is_st", sql)
        self.assertIn("SET is_suspended=true", sql)
        self.assertIn("SET adj_factor=%s", sql)
        self.assertIn("SET limit_up=%s,limit_down=%s", sql)

    def test_daily_suspension_without_resume_date_marks_only_that_day(self):
        class RecordingConnection:
            def __init__(self): self.calls = []
            def execute(self, sql, params=None):
                self.calls.append((" ".join(sql.split()), params))
                return MagicMock()

        connection = RecordingConnection()
        observed = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(normalize_tushare_rows(
            connection, "suspend_d",
            [{"ts_code": "600001.SH", "trade_date": "20260810", "suspend_timing": "全天"}],
            observed,
        ), 1)
        update = next(
            (sql, params) for sql, params in connection.calls
            if "UPDATE quant.canonical_bars_daily" in sql
        )
        self.assertIn("trading_date=%s", update[0])
        self.assertNotIn("trading_date >=", update[0])
        self.assertEqual(update[1], ("600001.SH", date(2026, 8, 10)))

    def test_adjustment_factor_removes_ex_right_price_jump_from_factor_returns(self):
        bars = [{"close": 10.0, "adj_factor": 1.0} for _ in range(5)]
        bars.append({"close": 5.0, "adj_factor": 2.0})
        self.assertEqual(factor_at(bars, 5, "momentum_5d"), 0.0)
        self.assertEqual(factor_at(bars, 5, "sma_gap_20d"), None)

    def test_limit_pattern_scales_for_chinext_and_bse(self):
        chinext = post_close_limit_daily_features([
            {"symbol": "300750.SZ", "trading_date": date(2026, 8, 11), "open": 10, "high": 12,
             "low": 8.2, "close": 12, "pre_close": 10, "volume": 100},
        ])
        bse = post_close_limit_daily_features([
            {"symbol": "830001.BJ", "trading_date": date(2026, 8, 11), "open": 10, "high": 13,
             "low": 7.4, "close": 13, "pre_close": 10, "volume": 100},
        ])
        self.assertEqual(chinext["limit_pct"], 20.0)
        self.assertTrue(chinext["ground_to_sky_daily_shape"])
        self.assertEqual(bse["limit_pct"], 30.0)
        self.assertTrue(bse["ground_to_sky_daily_shape"])

    def test_limit_pool_union_never_truncates_to_replay_samples(self):
        result = merge_limit_pool_sources(
            [{"row_data": {"ts_code": "600667.SH", "name": "太极实业", "tag": "首板"}, "provider_key": "tushare_super_sdk"}],
            [{"symbol": "600667.SH", "body": '{"名称":"太极实业","连板数":1,"炸板次数":0}', "source": "akshare"},
             {"symbol": "600162.SH", "body": '{"名称":"香江控股","连板数":1}', "source": "akshare"}],
        )
        self.assertEqual({item["ts_code"] for item in result["items"]}, {"600667.SH", "600162.SH"})
        self.assertEqual(result["coverage"]["union_count"], 2)
        self.assertEqual(result["coverage"]["intersection_count"], 1)
        taiji = next(item for item in result["items"] if item["ts_code"] == "600667.SH")
        self.assertEqual(len(taiji["sources"]), 2)
        self.assertEqual(taiji["open_num"], 0)

    def test_review_score_rewards_confirmed_evidence_and_penalizes_distribution(self):
        positive = strategy_pattern_review_score({
            "daily_features": {"volume_multiple_5d": 2.2},
            "board_context": {"exact_member_mapping": True, "net_amount": 1},
            "limit_context": {"streak_count": 3, "open_num": 1, "turnover_rate": 18,
                              "lhb_context": {"institution_net_buy": 10_000_000}},
        }, {"pattern_tags": ["opening_ladder_drive"]}, [])
        negative = strategy_pattern_review_score({
            "daily_features": {"volume_multiple_5d": 0.8},
            "board_context": {"exact_member_mapping": True, "net_amount": -1},
            "limit_context": {"streak_count": 1, "open_num": 20, "turnover_rate": 45,
                              "lhb_context": {"institution_net_buy": -10_000_000}},
        }, {"pattern_tags": []}, ["extreme_turnover", "lhb_institution_net_sell"])
        self.assertEqual(positive["review_tier"], "priority_review")
        self.assertGreater(positive["review_score"], negative["review_score"])

    def test_baostock_symbol_conversion(self):
        self.assertEqual(baostock_code("600519.SH"), "sh.600519")
        self.assertEqual(baostock_code("300750.SZ"), "sz.300750")

    def test_explicit_universe_is_normalized_and_gets_benchmark(self):
        self.assertEqual(resolve_sync_symbols(["300750.sz", "600519.SH", "invalid"]), ["000300.SH", "300750.SZ", "600519.SH"])

    def test_invalid_ohlc_is_rejected_before_raw_storage(self):
        with self.assertRaises(ValidationError):
            DailyBar(symbol="600519.SH", trading_date=date(2026, 8, 7), open="10", high="9", low="8", close="10")

    def test_complete_catalog_and_bounded_generic_request(self):
        # Supplier contract, observed additions, official point APIs, separately
        # licensed live APIs, and offline history are distinct inventory facts.
        self.assertEqual(len(SUPPLIER_109_CATALOG), 109)
        self.assertEqual(len(AUDITED_ADDITIONS_CATALOG), 7)
        self.assertEqual(len(TUSHARE_CATALOG), 200)
        self.assertEqual(catalog_counts()["market_hours_only"], 13)
        # stk_mins left the offline-only set on 2026-08-26 after a live
        # ProMax probe returned two years of 1-minute history.
        self.assertEqual(catalog_counts()["offline_files_only"], 5)
        self.assertIn("stk_auction", TUSHARE_CATALOG)
        self.assertIn("stk_auction_o", TUSHARE_CATALOG)
        self.assertIn("stk_auction_c", TUSHARE_CATALOG)
        self.assertIn("rt_min", TUSHARE_CATALOG)
        self.assertIn("stk_mins", TUSHARE_CATALOG)
        self.assertIn("moneyflow_cnt_ths", TUSHARE_CATALOG)
        self.assertIn("rt_min_daily", TUSHARE_CATALOG)
        self.assertIn("rt_etf_sz_iopv", REALTIME_MARKET_HOURS_APIS)
        self.assertIn("stk_mins", HISTORICAL_MINUTE_APIS)
        request = TushareFetchRequest(api_name="moneyflow", params={"ts_code": "000001.SZ", "start_date": "20260701", "end_date": "20260717"})
        self.assertEqual(request.max_rows, 500)
        calendar_request = TushareFetchRequest(
            api_name="trade_cal", params={"exchange": "SSE", "start_date": "20260101", "end_date": "20261231"}, max_rows=400,
        )
        self.assertEqual(calendar_request.max_rows, 400)
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="daily", params={"ts_code": "000001.SZ", "start_date": "20260101", "end_date": "20261231"})
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="daily", params={"start_date": "20260701", "end_date": "20260717"})
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="rt_min", params={"ts_code": "000001.SZ"})
        self.assertEqual(TushareFetchRequest(api_name="rt_min", params={"ts_code": "000001.SZ", "freq": "1MIN"}).api_name, "rt_min")
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="rt_min_daily", params={"ts_code": "000001.SZ"})
        self.assertEqual(TushareFetchRequest(api_name="rt_min_daily", params={"ts_code": "000001.SZ", "freq": "1MIN"}).api_name, "rt_min_daily")
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="stk_mins", params={"ts_code": "000001.SZ"})
        self.assertEqual(default_probe_params("rt_idx_min")["freq"], "1MIN")
        self.assertEqual(default_probe_params("rt_min_daily")["freq"], "1MIN")
        self.assertEqual(default_probe_params("rt_etf_min_daily")["freq"], "1MIN")
        self.assertEqual(default_probe_params("rt_idx_min_daily")["freq"], "1MIN")
        self.assertEqual(default_probe_params("rt_fut_min_daily", as_of=date(2026, 8, 10))["ts_code"], "IF2608.CFX")
        daily_probe = default_probe_params("daily", as_of=date(2026, 8, 7))
        self.assertEqual(daily_probe["ts_code"], "000636.SZ")
        self.assertEqual(daily_probe["start_date"], "20260731")
        self.assertEqual(default_probe_params("trade_cal", as_of=date(2026, 8, 7))["exchange"], "SSE")
        self.assertEqual(default_probe_params("ths_index")["type"], "N")
        self.assertEqual(default_probe_params("stock_hsgt", as_of=date(2026, 8, 7))["type"], "HK_SZ")
        self.assertEqual(default_probe_params("cn_gdp", as_of=date(2026, 8, 7))["q"], "2026Q3")
        self.assertEqual(default_probe_params("sge_basic")["ts_code"], "Au99.99.SGE")
        self.assertIsNone(default_probe_params("opt_daily"))
        self.assertIsNone(default_probe_params("ths_member"))
        self.assertEqual(TushareFetchRequest(api_name="ths_member", provider="super_get", params={"ts_code": "885573.TI"}).provider, "super_get")
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="ths_member", params={"ts_code": "885573"})
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="moneyflow_cnt_ths", params={})
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="fut_basic", params={"exchange": "SSE"})
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="ths_member", params={"ts_code": "885573.TI"}, require_complete=True)
        with self.assertRaises(ValidationError):
            TushareFetchRequest(api_name="ths_member", provider="super_get", params={"ts_code": "885573.TI"},
                                max_rows=10_000, paginate=True, require_complete=True)
        complete_members = TushareFetchRequest(
            api_name="ths_member", params={"ts_code": "885573.TI"}, max_rows=10_000,
            paginate=True, page_size=1000, require_complete=True,
        )
        self.assertTrue(complete_members.require_complete)
        self.assertEqual(provider_error_availability("您没有接口(hm_detail)访问权限"), "unsupported")
        self.assertEqual(provider_error_availability("parameter not allowed: type"), "unknown")

    def test_provider_error_detail_redacts_gateway_credentials(self):
        detail = safe_error_detail("HTTP X-API-Key: secret-key token=abc123 Authorization=Bearer value")
        self.assertNotIn("secret-key", detail)
        self.assertNotIn("value", detail)
        self.assertNotIn("abc123", detail)
        self.assertNotIn("Bearer value", detail)
        self.assertIn("<redacted>", detail)

    def test_historical_capacity_plan_is_estimate_only_and_keeps_minutes_offline(self):
        plan = historical_capacity_plan(years=3, universe_symbols=5500, trading_days_per_year=244, include_minute=False)
        self.assertEqual(plan["trading_days"], 732)
        self.assertGreater(plan["estimated_storage_gib"], 10)
        self.assertNotIn("minute_1m", {item["dataset"] for item in plan["datasets"]})
        minute_plan = historical_capacity_plan(years=3, universe_symbols=5500, trading_days_per_year=244, include_minute=True)
        minute = [item for item in minute_plan["datasets"] if item["dataset"] == "minute_1m"][0]
        self.assertEqual(minute["priority"], "offline_only")
        self.assertGreater(minute_plan["estimated_storage_gib"], plan["estimated_storage_gib"])

    def test_provider_routing_is_capability_scoped(self):
        env = {
            "TUSHARE_PRIMARY_TOKEN": "primary", "TUSHARE_PRIMARY_API_URL": "https://primary.example",
            "TUSHARE_SUPER_TOKEN": "super", "TUSHARE_SUPER_API_URL": "https://super.example",
            "TUSHARE_SUPER_PROXY_URL": "http://proxy.example:8080",
            "TUSHARE_SUPER_REALTIME_API_KEY": "live", "TUSHARE_SUPER_REALTIME_API_URL": "https://realtime.example",
            "TUSHARE_SUPER_REALTIME_PROXY_URL": "http://realtime-proxy.example:8080",
            "TUSHARE_BACKUP_API_KEY": "backup", "TUSHARE_BACKUP_API_URL": "https://backup.example",
        }
        configs = provider_configs(env)
        self.assertTrue(configs["primary"].configured)
        self.assertEqual(configs["super_sdk"].protocol, "sdk_path")
        self.assertEqual(configs["super_sdk"].proxy_url, "http://proxy.example:8080")
        self.assertEqual(configs["super_get"].protocol, "get_x_api_key")
        self.assertTrue(configs["super_get"].uses_super_get("rt_min"))
        self.assertTrue(configs["super_get"].uses_super_get("daily"))
        self.assertTrue(configs["super_get"].uses_super_get("moneyflow"))
        self.assertTrue(configs["super_get"].uses_super_get("stk_factor_pro"))
        self.assertEqual(configs["super_get"].proxy_url, "http://realtime-proxy.example:8080")
        self.assertEqual(configs["primary"].rate_limit_per_minute, 60)
        self.assertEqual(configs["super_sdk"].rate_limit_per_minute, 30)
        self.assertEqual(configs["super_get"].rate_limit_per_minute, 60)
        self.assertEqual(configs["super_get"].min_interval_seconds, 1.0)
        self.assertEqual([item.key for item in provider_candidates("daily", environ=env)], ["tushare_super_get", "tushare_primary"])
        self.assertEqual([item.key for item in provider_candidates("stock_basic", environ=env)], ["tushare_primary", "tushare_super_get", "tushare_super_sdk", "tushare_backup"])
        self.assertEqual([item.key for item in provider_candidates("stk_factor", environ=env)], ["tushare_primary", "tushare_super_sdk"])
        self.assertEqual([item.key for item in provider_candidates("moneyflow", environ=env)], ["tushare_super_sdk", "tushare_super_get", "tushare_primary"])
        self.assertEqual([item.key for item in provider_candidates("ths_member", environ=env)], ["tushare_super_sdk", "tushare_super_get", "tushare_primary"])
        self.assertEqual([item.key for item in provider_candidates("moneyflow_ind_dc", environ=env)], ["tushare_super_get", "tushare_super_sdk", "tushare_primary"])
        self.assertEqual([item.key for item in provider_candidates("rt_min", environ=env)], ["tushare_super_sdk", "tushare_super_get"])
        self.assertEqual([item.key for item in provider_candidates("rt_min_daily", environ=env)], ["tushare_super_get"])
        self.assertEqual([item.key for item in provider_candidates("rt_etf_min", environ=env)], ["tushare_super_sdk"])
        self.assertEqual([item.key for item in provider_candidates("rt_idx_min", environ=env)], ["tushare_super_sdk"])
        self.assertEqual([item.key for item in provider_candidates("rt_sw_k", environ=env)], ["tushare_super_get", "tushare_super_sdk"])
        self.assertEqual([item.key for item in provider_candidates("rt_fut_min", environ=env)], ["tushare_super_get"])
        self.assertEqual(provider_candidates("rt_etf_min_daily", environ=env), [])
        self.assertEqual([item.key for item in provider_candidates("index_weight", environ=env)], ["tushare_super_sdk", "tushare_primary"])
        self.assertEqual([item.key for item in provider_candidates("daily", "super_sdk", environ=env)], ["tushare_super_sdk"])
        status = {item["name"]: item for item in provider_status(environ=env)}
        self.assertEqual(status["primary"]["realtime_coverage"], "unavailable")
        self.assertEqual(status["super_sdk"]["realtime_coverage"], "verified_partial")
        self.assertEqual(status["super_get"]["realtime_coverage"], "verified_partial")
        self.assertEqual(status["super_get"]["get_apis"], sorted(SUPER_GET_VERIFIED_APIS))
        self.assertEqual(status["super_get"]["bounded_only_apis"], ["ths_index", "ths_member"])
        self.assertEqual(status["super_get"]["reconciliation_required_apis"], ["stock_basic", "top_inst", "top_list"])
        self.assertIn("rt_min", status["super_sdk"]["super_alias_first_apis"])
        self.assertNotIn("rt_min", status["super_get"]["super_alias_first_apis"])
        self.assertIn("rt_min_daily", status["super_get"]["super_alias_first_apis"])
        self.assertNotIn("stock_basic", status["super_get"]["complete_query_apis"])

    def test_promax_get_is_fail_closed_to_its_verified_subset(self):
        env = {
            "TUSHARE_PRIMARY_TOKEN": "primary", "TUSHARE_PRIMARY_API_URL": "https://primary.example",
            "TUSHARE_SUPER_GET_MODE": "promax",
            "TUSHARE_SUPER_GET_API_KEY": "promax", "TUSHARE_SUPER_GET_API_URL": "https://promax.example",
            "TUSHARE_SUPER_REALTIME_API_KEY": "legacy", "TUSHARE_SUPER_REALTIME_PROXY_URL": "http://legacy-proxy.example",
        }
        promax = provider_configs(env)["super_get"]
        self.assertEqual(promax.label, "Tushare ProMax GET 网关")
        self.assertEqual(promax.credential, "promax")
        self.assertEqual(promax.proxy_url, "")
        self.assertTrue(promax.supports("daily"))
        self.assertTrue(promax.supports("rt_min_daily"))
        self.assertTrue(promax.supports("moneyflow"))
        # Re-probed 2026-08-26 with retries: both answered with real rows, so
        # both are now routable.  The gateway stays fail-closed on the one
        # route that never answered and on anything undeclared.
        self.assertTrue(promax.supports("ths_member"))
        self.assertTrue(promax.supports("rt_fut_min"))
        self.assertFalse(promax.supports("rt_fut_min_daily"))
        self.assertFalse(promax.supports("not_a_real_api"))
        self.assertEqual([item.key for item in provider_candidates("daily", environ=env)],
                         ["tushare_super_get", "tushare_primary"])
        self.assertEqual([item.key for item in provider_candidates("rt_min_daily", environ=env)],
                         ["tushare_super_get"])
        status = {item["name"]: item for item in provider_status(environ=env)}["super_get"]
        self.assertEqual(status["get_gateway_mode"], "promax")
        self.assertEqual(status["get_apis"], sorted(PROMAX_VERIFIED_APIS))
        self.assertNotIn("rt_fut_min_daily", status["get_apis"])

    def test_realtime_cross_section_is_filtered_to_requested_symbol(self):
        rows = [
            {"ts_code": "801010.SI", "name": "农林牧渔"},
            {"ts_code": "801020.SI", "name": "采掘"},
        ]
        self.assertEqual(
            _filter_requested_realtime_rows("rt_sw_k", {"ts_code": "801020.SI"}, rows),
            [{"ts_code": "801020.SI", "name": "采掘"}],
        )
        self.assertEqual(_filter_requested_realtime_rows("daily", {"ts_code": "801020.SI"}, rows), rows)

    def test_valid_empty_preferred_provider_falls_back_without_merging(self):
        env = {
            "TUSHARE_SUPER_TOKEN": "city", "TUSHARE_SUPER_API_URL": "https://city.example",
            "TUSHARE_SUPER_REALTIME_API_KEY": "get", "TUSHARE_SUPER_REALTIME_API_URL": "https://get.example",
        }
        configs = provider_configs(env)

        async def provider_call(provider, _api_name, _params, _fields):
            if provider.name == "super_sdk":
                return []
            return [{"ts_code": "000001.SZ", "trade_date": "20260811", "turnover_rate": 1.2}]

        with patch("app.tushare_providers.provider_candidates", return_value=[configs["super_sdk"], configs["super_get"]]), \
             patch("app.tushare_providers.call_provider", new=AsyncMock(side_effect=provider_call)):
            result = asyncio.run(call_with_fallback("daily_basic", {"trade_date": "20260811"}, None, "super"))
        self.assertEqual(result.provider.name, "super_get")
        self.assertEqual(result.empty_providers, ("tushare_super_sdk",))
        self.assertEqual(result.rows, [{"ts_code": "000001.SZ", "trade_date": "20260811", "turnover_rate": 1.2}])

    def test_circuit_excludes_an_open_provider_from_fallback_order(self):
        env = {
            "TUSHARE_SUPER_TOKEN": "city", "TUSHARE_SUPER_API_URL": "https://city.example",
            "TUSHARE_SUPER_REALTIME_API_KEY": "get", "TUSHARE_SUPER_REALTIME_API_URL": "https://get.example",
        }
        configs = provider_configs(env)
        with patch("app.tushare_providers.provider_candidates", return_value=[configs["super_sdk"], configs["super_get"]]), \
             patch("app.tushare_providers.call_provider", new=AsyncMock(return_value=[{"ts_code": "000001.SZ"}])) as call:
            result = asyncio.run(call_with_fallback("daily", {}, None, "super", blocked_provider_keys={"tushare_super_sdk"}))
        self.assertEqual(result.provider.key, "tushare_super_get")
        self.assertEqual(call.await_args.args[0].key, "tushare_super_get")

    def test_transient_provider_http_status_is_retried_once(self):
        provider = provider_configs({"TUSHARE_PRIMARY_TOKEN": "token", "TUSHARE_PRIMARY_API_URL": "https://primary.example"})["primary"]
        transient, success = MagicMock(status_code=503), MagicMock(status_code=200)
        transient.headers = {"Retry-After": "3"}
        operation = AsyncMock(side_effect=[transient, success])
        with patch("app.tushare_providers.request_limiter.acquire", new=AsyncMock()), \
             patch("app.tushare_providers.asyncio.sleep", new=AsyncMock()) as sleep:
            response = asyncio.run(provider_http_request(provider, operation))
        self.assertIs(response, success)
        self.assertEqual(operation.await_count, 2)
        sleep.assert_awaited_once_with(3.0)

    def test_retry_after_hint_is_bounded_and_never_reduces_backoff(self):
        self.assertEqual(retry_delay_seconds({"Retry-After": "3"}, 0.8), 3.0)
        self.assertEqual(retry_delay_seconds({"Retry-After": "0"}, 0.8), 0.8)
        self.assertEqual(retry_delay_seconds({"Retry-After": "999"}, 0.8), 10.0)
        self.assertEqual(retry_delay_seconds({"Retry-After": "invalid"}, 0.8), 0.8)

    def test_shared_provider_rate_reservation_is_bounded_and_atomic(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"wait_seconds": 1.25}
        wait = reserve_provider_rate_limit_slot(connection, "tushare_super_get", 1.0, 5.0)
        self.assertEqual(wait, 1.25)
        sql, params = connection.execute.call_args.args
        self.assertIn("ON CONFLICT(provider_key)", sql)
        self.assertIn("WHERE quant.provider_rate_limit_slots.next_allowed_at", sql)
        self.assertEqual(params, ("tushare_super_get", 1.0, 1.0, 5.0, 1.0))
        connection.execute.return_value.fetchone.return_value = None
        self.assertIsNone(reserve_provider_rate_limit_slot(connection, "tushare_super_get", 1.0, 5.0))
        self.assertEqual(provider_request_spacing_seconds(60, 0.0), 1.0)
        self.assertEqual(provider_request_spacing_seconds(30, 0.0), 2.0)
        self.assertEqual(provider_request_spacing_seconds(60, 3.0), 3.0)
        self.assertEqual(provider_global_rate_limit_max_wait_seconds({"QUANT_PROVIDER_GLOBAL_RATE_LIMIT_MAX_WAIT_SECONDS": "999"}), 30.0)

    def test_shared_provider_reserver_precedes_the_local_limiter(self):
        provider = provider_configs({"TUSHARE_PRIMARY_TOKEN": "token", "TUSHARE_PRIMARY_API_URL": "https://primary.example"})["primary"]
        sequence: list[str] = []

        async def reserve(provider_key: str, rate: int, interval: float) -> None:
            self.assertEqual((provider_key, rate, interval), (provider.key, provider.rate_limit_per_minute, provider.min_interval_seconds))
            sequence.append("shared")

        async def exercise() -> None:
            configure_provider_request_reserver(reserve)
            try:
                with patch("app.tushare_providers.request_limiter.acquire", new=AsyncMock(side_effect=lambda *_, **__: sequence.append("local"))):
                    await acquire_provider_request_slot(provider)
            finally:
                configure_provider_request_reserver(None)

        asyncio.run(exercise())
        self.assertEqual(sequence, ["shared", "local"])
        self.assertFalse(provider_request_reservation_status()["shared_database_reservation"])

    def test_lifespan_reserver_waits_for_an_allocated_slot_or_rejects_locally(self):
        async def exercise() -> None:
            captured_actions = []

            async def reserve_slot(action, *args, **kwargs):
                captured_actions.append((action, args, kwargs))
                return 1.25

            with patch("app.main.run_database_blocking", new=AsyncMock(side_effect=reserve_slot)) as reserve, \
                 patch("app.main.asyncio.sleep", new=AsyncMock()) as sleep, \
                 patch("app.main.provider_shared_rate_limit_wait_seconds") as wait_metric:
                await reserve_tushare_provider_request_slot("tushare_super_get", 60, 1.0)
                reserve.assert_awaited_once()
                action, args, kwargs = captured_actions[0]
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {"timeout_seconds": 5})
                self.assertEqual(action.__name__, "reserve")
                sleep.assert_awaited_once_with(1.25)
                wait_metric.labels.assert_called_once_with("tushare_super_get")
                wait_metric.labels.return_value.observe.assert_called_once_with(1.25)
            with patch("app.main.run_database_blocking", new=AsyncMock(return_value=None)), \
                 patch("app.main.provider_shared_rate_limit_rejections_total") as rejection_metric:
                with self.assertRaises(ExecutorSaturatedError):
                    await reserve_tushare_provider_request_slot("tushare_super_get", 60, 1.0)
                rejection_metric.labels.assert_called_once_with("tushare_super_get")
                rejection_metric.labels.return_value.inc.assert_called_once_with()

        asyncio.run(exercise())

    def test_ths_member_sdk_duplicate_layout_is_repaired_and_deduplicated(self):
        rows = _normalize_ths_member_rows([
            {"ts_code": "885338.TI", "con_code": "000001.SZ", "con_name": None, "is_new": "平安银行"},
            {"ts_code": "885338.TI", "con_code": "000001.SZ", "con_name": "平安银行", "is_new": None},
            {"ts_code": "885338.TI", "con_code": "000002.SZ", "con_name": "万科A", "is_new": "Y"},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["con_name"], "平安银行")
        self.assertIsNone(rows[0]["is_new"])
        self.assertEqual(rows[1]["is_new"], "Y")

    def test_ths_membership_count_excludes_historical_constituents(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, query, params):
                self.calls.append((query, params))

        connection = Connection()
        observed_at = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        rows = [
            {"con_code": "000001.SZ", "in_date": "20200101", "out_date": None},
            {"con_code": "000002.SZ", "in_date": "20200101", "out_date": "20250701"},
        ]
        with patch("app.main.ensure_tushare_instrument"):
            members = persist_ths_sector_members(
                connection, "ths_concept_flow", "885001.TI", rows,
                "tushare_super_sdk", observed_at,
            )
        self.assertEqual(members, 1)
        self.assertEqual(connection.calls[-1][1][-1], ["000001.SZ"])

    def test_paginated_provider_call_requires_a_terminal_page_from_one_source(self):
        env = {
            "TUSHARE_SUPER_REALTIME_API_KEY": "live",
            "TUSHARE_SUPER_REALTIME_API_URL": "https://realtime.example",
        }
        provider = provider_configs(env)["super_get"]

        async def page_call(_provider, _api_name, params, _fields):
            return ([{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}]
                    if params["offset"] == 0 else [{"ts_code": "000003.SZ"}])

        with patch("app.tushare_providers.provider_candidates", return_value=[provider]), \
             patch("app.tushare_providers.call_provider", new=AsyncMock(side_effect=page_call)):
            result = asyncio.run(call_with_fallback(
                "stock_basic", {}, None, "super_get", paginate=True,
                page_size=2, max_rows=10, max_pages=5,
            ))
        self.assertTrue(result.complete)
        self.assertEqual(result.pages, 2)
        self.assertEqual(len(result.rows), 3)

    def test_realtime_guard_accepts_only_continuous_auction_sessions(self):
        self.assertTrue(china_equity_session(__import__("datetime").datetime(2026, 8, 10, 10, 0, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))))[0])
        self.assertFalse(china_equity_session(__import__("datetime").datetime(2026, 8, 10, 12, 0, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))))[0])
        self.assertFalse(china_equity_session(__import__("datetime").datetime(2026, 8, 9, 10, 0, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))))[0])
        self.assertTrue(china_futures_session(__import__("datetime").datetime(2026, 8, 10, 9, 15, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))))[0])
        self.assertFalse(china_futures_session(__import__("datetime").datetime(2026, 8, 10, 12, 0, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))))[0])

    def test_intraday_high_frequency_windows_keep_board_refresh_bounded(self):
        china = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        high = __import__("datetime").datetime(2026, 8, 10, 9, 45, tzinfo=china)
        normal = __import__("datetime").datetime(2026, 8, 10, 10, 15, tzinfo=china)
        opening = __import__("datetime").datetime(2026, 8, 10, 9, 30, tzinfo=china)
        opening_end = __import__("datetime").datetime(2026, 8, 10, 10, 0, tzinfo=china)
        late_morning = __import__("datetime").datetime(2026, 8, 10, 11, 10, tzinfo=china)
        afternoon_open = __import__("datetime").datetime(2026, 8, 10, 13, 0, tzinfo=china)
        closing_window = __import__("datetime").datetime(2026, 8, 10, 14, 30, tzinfo=china)
        self.assertTrue(intraday_high_frequency_window(high))
        self.assertFalse(intraday_high_frequency_window(normal))
        self.assertTrue(intraday_high_frequency_window(opening))
        self.assertFalse(intraday_high_frequency_window(opening_end))
        self.assertTrue(intraday_high_frequency_window(late_morning))
        self.assertTrue(intraday_high_frequency_window(afternoon_open))
        self.assertTrue(intraday_high_frequency_window(closing_window))
        self.assertEqual(intraday_effective_scan_interval_seconds(30, high), 10)
        self.assertEqual(intraday_effective_scan_interval_seconds(30, opening), 10)
        self.assertEqual(intraday_effective_scan_interval_seconds(30, normal), 30)
        self.assertEqual(intraday_effective_scan_interval_seconds(0, high), 0)
        offsets = [0]
        for _ in range(6):
            offsets.append(intraday_next_realtime_validation_offset(offsets[-1], 4))
        self.assertEqual(offsets, [0, 4, 8, 12, 16, 0, 4])
        self.assertTrue(all(0 <= offset < 20 for offset in offsets))
        self.assertEqual(intraday_next_realtime_validation_offset(12, 0), 12)
        self.assertEqual(intraday_next_realtime_validation_offset(36, 4, slots=40), 0)
        symbols = [f"{index:06d}.SH" for index in range(36)]
        offset = 0
        requested: list[str] = []
        for _ in range(9):
            selected, offset = intraday_realtime_validation_slice(symbols, offset, 4)
            self.assertEqual(len(selected), 4)
            requested.extend(selected)
        self.assertEqual(set(requested), set(symbols))
        self.assertEqual(len(requested), len(symbols))
        self.assertEqual(offset, 0)
        self.assertEqual(intraday_realtime_validation_slice(symbols, 35, 0), ([], 35))
        self.assertEqual(intraday_realtime_validation_slice([], 3, 4), ([], 0))
        self.assertEqual(next_rotation_offset_from_scan({"realtime_validation": {"next_offset": 8}}, 4), 8)
        self.assertEqual(next_rotation_offset_from_scan(RuntimeError("upstream unavailable"), 4), 4)
        self.assertEqual(next_rotation_offset_from_scan({"realtime_validation": {"next_offset": 41}}, 4), 4)
        self.assertEqual(next_rotation_offset_from_scan({"realtime_validation": {"next_offset": True}}, 4), 4)
        self.assertEqual(fast_quote_rotation_slot(["000001.SZ", "000002.SZ"], 2), ("000001.SZ", 3))
        self.assertEqual(fast_quote_rotation_slot([], 3), (None, 3))
        # A full rotation through the pool must fit inside the declared
        # freshness budget: one new symbol starts per interval tick.
        self.assertEqual(bounded_rotation_pool_size(40, 1.0, 30.0), 30)
        self.assertEqual(bounded_rotation_pool_size(20, 1.0, 30.0), 20)
        self.assertEqual(bounded_rotation_pool_size(40, 1.0, None), 40)
        self.assertEqual(bounded_rotation_pool_size(40, 0.0, 30.0), 40)
        self.assertEqual(intraday_board_refresh_interval_seconds(high), 60)
        self.assertEqual(intraday_board_refresh_interval_seconds(normal), 300)
        pre_open = __import__("datetime").datetime(2026, 8, 10, 9, 29, 50, tzinfo=china)
        self.assertEqual(intraday_next_monitor_delay_seconds(30, pre_open), 10.0)
        one_second_to_open = __import__("datetime").datetime(2026, 8, 10, 9, 29, 59, tzinfo=china)
        self.assertEqual(intraday_next_monitor_delay_seconds(30, one_second_to_open), 1.0)
        with patch.dict("os.environ", {"INTRADAY_SUPER_GET_FAST_INTERVAL_SECONDS": "1"}):
            self.assertEqual(intraday_super_get_fast_interval_seconds(), 1.0)
        with patch.dict("os.environ", {"INTRADAY_SUPER_GET_FAST_MAX_IN_FLIGHT": "20"}):
            self.assertEqual(intraday_super_get_fast_max_in_flight(), 20)
        with patch.dict("os.environ", {"INTRADAY_SUPER_GET_FAST_MAX_SYMBOLS": "40"}):
            self.assertEqual(intraday_super_get_fast_max_symbols(), 40)
        with patch.dict("os.environ", {"INTRADAY_FAST_QUOTE_RETENTION_DAYS": "7"}):
            self.assertEqual(intraday_fast_quote_retention_days(), 7)
        with patch.dict("os.environ", {"INTRADAY_BOARD_ROTATION_RETENTION_DAYS": "60"}):
            self.assertEqual(intraday_board_rotation_retention_days(), 60)

    def test_post_close_strategy_retry_window_is_shanghai_and_bounded(self):
        china = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        self.assertFalse(post_close_strategy_retry_window(__import__("datetime").datetime(2026, 8, 10, 18, 54, tzinfo=china)))
        self.assertTrue(post_close_strategy_retry_window(__import__("datetime").datetime(2026, 8, 10, 18, 55, tzinfo=china)))
        self.assertTrue(post_close_strategy_retry_window(__import__("datetime").datetime(2026, 8, 10, 20, 29, 59, tzinfo=china)))
        self.assertFalse(post_close_strategy_retry_window(__import__("datetime").datetime(2026, 8, 10, 20, 30, tzinfo=china)))

    def test_intraday_board_curve_deduplicates_one_board_per_minute(self):
        rows = [
            {"行业": "芯片概念", "行业代码": "BK0917", "行业-涨跌幅": "1.2%", "流入资金": "120", "流出资金": "30"},
            {"行业": "芯片概念", "行业代码": "BK0917", "行业-涨跌幅": "1.4%", "流入资金": "122", "流出资金": "30"},
            {"行业": "小金属", "行业代码": "BK1027", "行业-涨跌幅": "-0.5%", "净额": "-20"},
        ]
        items = intraday_board_flow_curve_items("concept", rows)
        self.assertEqual(len(items), 2)
        chip = next(item for item in items if item["sector_key"] == "BK0917")
        self.assertEqual(chip["taxonomy_key"], "eastmoney_concept")
        self.assertEqual(chip["net_inflow"], 91.0)
        self.assertEqual(chip["change_pct"], 1.3)

    def test_board_rotation_requires_large_same_source_delta_then_retained_direction(self):
        previous = [
            {"taxonomy_key": "eastmoney_concept", "sector_key": f"C{index}", "label": f"概念{index}", "net_inflow": 0.2}
            for index in range(24)
        ] + [
            {"taxonomy_key": "eastmoney_concept", "sector_key": "CROSS", "label": "交叉概念", "net_inflow": -3.1},
            {"taxonomy_key": "eastmoney_industry", "sector_key": "SURGE", "label": "加速行业", "net_inflow": 1.2},
        ]
        current = [
            {**item, "net_inflow": 0.3} for item in previous if item["sector_key"] not in {"CROSS", "SURGE"}
        ] + [
            {"taxonomy_key": "eastmoney_concept", "sector_key": "CROSS", "label": "交叉概念", "net_inflow": 3.4, "change_pct": 1.2},
            {"taxonomy_key": "eastmoney_industry", "sector_key": "SURGE", "label": "加速行业", "net_inflow": 7.2, "change_pct": 0.8},
        ]
        candidates = board_rotation_candidates(previous, current)
        cross = next(item for item in candidates if item["sector_key"] == "CROSS")
        surge = next(item for item in candidates if item["sector_key"] == "SURGE")
        self.assertEqual(cross["event_type"], "cross_zero")
        self.assertEqual(cross["direction"], "inflow")
        self.assertEqual(surge["event_type"], "flow_surge")
        self.assertTrue(board_rotation_still_directional(cross, current))
        self.assertFalse(board_rotation_still_directional(cross, [{**current[-2], "net_inflow": -0.2}]))
        text = board_rotation_alert_text({**cross, "observed_at_shanghai": "2026-08-12 09:32"})
        self.assertIn("流出转流入", text)
        self.assertIn("下一分钟方向确认", text)

    def test_intraday_board_curve_uses_sse_clock_from_0920(self):
        china = ZoneInfo("Asia/Shanghai")
        pre_open = datetime(2026, 8, 10, 9, 20, tzinfo=china)
        self.assertTrue(intraday_board_curve_clock_session(pre_open)[0])
        lunch = datetime(2026, 8, 10, 12, 0, tzinfo=china)
        self.assertFalse(intraday_board_curve_clock_session(lunch)[0])
        slots = intraday_board_display_slots(date(2026, 8, 10), lunch)
        self.assertEqual(len(slots), 131)
        self.assertEqual(slots[0].astimezone(china).strftime("%H:%M"), "09:20")
        self.assertEqual(slots[-1].astimezone(china).strftime("%H:%M"), "11:30")

    def test_fast_super_get_quote_confirms_or_vetoes_fresh_tencent_price(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        confirmed = intraday_fast_quote_confirmation(
            {"price": 10.0}, {"price": 10.05, "observed_at": now}, now,
        )
        mismatch = intraday_fast_quote_confirmation(
            {"price": 10.0}, {"price": 10.9, "observed_at": now}, now,
        )
        stale = intraday_fast_quote_confirmation(
            {"price": 10.0}, {"price": 10.0, "observed_at": now},
            now + __import__("datetime").timedelta(seconds=31),
        )
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertEqual(stale["status"], "stale")

    def test_runtime_service_health_distinguishes_standby_starting_and_stale(self):
        china = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        before_open = datetime(2026, 8, 11, 9, 20, tzinfo=china)
        just_opened = datetime(2026, 8, 11, 9, 30, 20, tzinfo=china)
        running = datetime(2026, 8, 11, 9, 40, tzinfo=china)
        self.assertEqual(intraday_runtime_service_state(
            configured=True, expected_active=False, last_observed_at=None,
            observed_at=before_open, max_age_seconds=30,
        )[0], "standby")
        self.assertEqual(intraday_runtime_service_state(
            configured=True, expected_active=True, last_observed_at=None,
            observed_at=just_opened, max_age_seconds=30,
        )[0], "starting")
        self.assertEqual(intraday_runtime_service_state(
            configured=True, expected_active=True, last_observed_at=running - __import__("datetime").timedelta(seconds=10),
            observed_at=running, max_age_seconds=30,
        )[0], "healthy")
        self.assertEqual(intraday_runtime_service_state(
            configured=True, expected_active=True, last_observed_at=running - __import__("datetime").timedelta(seconds=90),
            observed_at=running, max_age_seconds=30,
        )[0], "degraded")

    def test_provider_rate_limiter_enforces_minimum_start_spacing(self):
        limiter = ProviderRateLimiter()

        async def exercise():
            loop = asyncio.get_running_loop()
            started = loop.time()
            await limiter.acquire("test", 600, 0.05)
            await limiter.acquire("test", 600, 0.05)
            return loop.time() - started

        self.assertGreaterEqual(asyncio.run(exercise()), 0.045)

    def test_bulk_requests_cannot_starve_the_realtime_reservation(self):
        self.assertEqual(realtime_reserved_slots(600), 150)
        self.assertEqual(realtime_reserved_slots(3), 0)
        self.assertEqual(realtime_reserved_slots(4), 1)
        limiter = ProviderRateLimiter()

        async def exercise() -> tuple[bool, bool]:
            # Exhaust everything except the realtime reservation with bulk calls.
            for _ in range(3):
                await limiter.acquire("test", 4, capability_class="bulk")
            bulk_admitted = True
            try:
                await asyncio.wait_for(limiter.acquire("test", 4, capability_class="bulk"), timeout=0.1)
            except asyncio.TimeoutError:
                bulk_admitted = False
            realtime_admitted = True
            try:
                await asyncio.wait_for(limiter.acquire("test", 4, capability_class="realtime"), timeout=0.1)
            except asyncio.TimeoutError:
                realtime_admitted = False
            return bulk_admitted, realtime_admitted

        bulk_admitted, realtime_admitted = asyncio.run(exercise())
        self.assertFalse(bulk_admitted, "bulk must not be able to consume the reserved realtime slot")
        self.assertTrue(realtime_admitted, "realtime must still get in even after bulk exhausts its own share")

    def test_super_get_session_reuses_proxy_pool_per_worker_thread(self):
        session = MagicMock()
        values = []

        def worker():
            values.append(_super_get_session("http://proxy.example:8080"))
            values.append(_super_get_session("http://proxy.example:8080"))

        with patch("app.tushare_providers.requests.Session", return_value=session) as constructor:
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        self.assertEqual(values, [session, session])
        constructor.assert_called_once_with()
        self.assertFalse(session.trust_env)
        session.proxies.update.assert_called_once_with({
            "http": "http://proxy.example:8080", "https": "http://proxy.example:8080",
        })
        self.assertEqual(session.mount.call_count, 2)
        capacity = super_get_executor_status()
        self.assertGreaterEqual(capacity["workers"], 1)
        self.assertGreaterEqual(capacity["queue_capacity"], 0)
        self.assertEqual(capacity["occupied"], 0)

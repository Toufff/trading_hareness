import asyncio
import threading
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from app.main import ConceptMemberSyncRequest, DailyBar, EastmoneyBoardMemberSyncRequest, IntradayScanRequest, IntradaySectorReportRequest, MarketSnapshotRequest, OfflineMinuteImportRequest, SectorCatalogSyncRequest, StrategyPatternMiningRequest, TushareFetchRequest, UniverseUpdateRequest, annotate_intraday_flow_percentiles, baostock_code, build_market_snapshot, call_tushare_api, china_equity_session, china_futures_session, cn_today, eastmoney_member_symbol, historical_capacity_plan, intraday_board_curve_clock_session, intraday_board_display_slots, intraday_board_flow_curve_items, intraday_board_refresh_interval_seconds, intraday_board_rotation_retention_days, intraday_eac_acceptance_assessment, intraday_effective_scan_interval_seconds, intraday_fast_quote_confirmation, intraday_fast_quote_retention_days, intraday_high_frequency_window, intraday_minute_features, intraday_next_monitor_delay_seconds, intraday_next_realtime_validation_offset, intraday_outcome_attribution_summary, intraday_peer_context, intraday_point_in_time_market_context_batch, intraday_quote_from_tencent, intraday_runtime_service_state, intraday_sector_report, intraday_signal_attribution, intraday_signal_event_state, intraday_signal_rules, intraday_super_get_fast_interval_seconds, intraday_super_get_fast_max_in_flight, legacy_schema_bootstrap_enabled, looks_like_response_header, market_snapshot_public_quote_settings, merge_intraday_sina_watch_quotes, merge_intraday_watch_quote_prices, normalize_tushare_rows, offline_minute_row, open_provider_capabilities, persist_ths_sector_members, provider_error_availability, provider_global_rate_limit_max_wait_seconds, realtime_rows_are_current, record_provider_failure, reserve_tushare_provider_request_slot, resolve_sync_symbols, resolve_sync_symbols_async, retry_pending_board_rotation_alerts, run_strategy_pattern_mining, sse_calendar_open_async, strategy_index_regime, strategy_intraday_candidates, strategy_market_regime, strategy_market_state, strategy_rank, technical_summary, tencent_snapshot_quotes, ths_concept_top_stocks, ths_taxonomy_key, write_access_allowed
from app.factor_lab import factor_at
from app.market_rules import a_share_limit_ratio, is_st_security_name
from app.intraday_alerts import daily_strategy_summary_text, delivery_health_recovery_text, intraday_alert_text
from app.board_rotation import board_rotation_alert_text, board_rotation_candidates, board_rotation_still_directional
from app.board_stock_mining import board_stock_mining_candidates
from app.limit_linkage_mining import limit_linkage_candidates
from app.free_market_providers import _tencent_order_book_row
from app.order_book_features import aggregate_order_book_observations, order_book_observation
from app.intraday_outcomes import a_share_return_decomposition
from app.board_curve_read_model import board_display_slots, intraday_board_flow_curves as read_intraday_board_flow_curves, latest_close_sector_review_report as read_latest_close_sector_review_report
from app.research_catalog_read_model import data_quality_issues as read_data_quality_issues, factor_evaluations as read_factor_evaluations, latest_features as read_latest_features, strategy_experiments as read_strategy_experiments
from app.intraday_outcome_read_model import latest_intraday_outcomes as read_latest_intraday_outcomes
from app.sector_read_model import market_sectors as read_market_sectors, sector_members as read_sector_members
from app.intraday_evidence_read_model import latest_scan as read_latest_intraday_scan
from app.market_result_read_model import market_snapshots as read_market_snapshots, tushare_raw as read_tushare_raw
from app.http_clients import alert_http_client, alert_http_client_status, close_http_clients, provider_http_client, provider_http_client_status, public_http_client, public_http_client_status, start_http_clients
from app.intraday_runtime_status import load_intraday_runtime_evidence
from app.intraday_status_read_model import IntradayStatusDependencies, intraday_services_status_payload as read_intraday_services_status_payload
from app.health_read_model import DatabaseUnavailableError, HealthDependencies, health_payload as read_health_payload
from app.alert_transport import post_feishu_alert_text
from app.provider_observability import provider_health_item, provider_health_snapshot, provider_health_summary
from app.runtime_tasks import observe_completed_task, supervise_leased_loop, supervise_loop
from app.runtime_resources import bounded_memory_ratio, bounded_min_free_bytes, runtime_resource_state
from app.runtime_executors import ExecutorSaturatedError
from app.provider_catalog import tushare_catalog_snapshot
from app.routers.provider_status import build_provider_status_router
from app.routers.strategy_pattern_reads import build_strategy_pattern_reads_router
from app.routers.research_readiness import build_research_readiness_router, training_roadmap_payload
from app.routers.intraday_status import build_intraday_status_router
from app.routers.analyst_reads import build_analyst_reads_router
from app.routers.analyst_trade_action_reads import build_analyst_trade_action_reads_router
from app.routers.analyst_skill_reads import build_analyst_skill_reads_router
from app.routers.analyst_research_reads import build_analyst_research_reads_router
from app.routers.event_reads import build_event_reads_router
from app.routers.strategy_reads import build_strategy_reads_router
from app.routers.board_rotation_reads import build_board_rotation_reads_router
from app.routers.board_curve_reads import build_board_curve_reads_router
from app.routers.research_catalog_reads import build_research_catalog_reads_router
from app.routers.intraday_outcome_reads import build_intraday_outcome_reads_router
from app.routers.sector_reads import build_sector_reads_router
from app.routers.intraday_evidence_reads import build_intraday_evidence_reads_router
from app.routers.market_result_reads import build_market_result_reads_router
from app.routers.provider_actions import ProviderActionDependencies, build_provider_actions_router
from app.routers.market_actions import MarketActionDependencies, build_market_actions_router
from app.routers.intraday_actions import IntradayActionDependencies, build_intraday_actions_router
from app.routers.sector_actions import SectorActionDependencies, build_sector_actions_router
from app.routers.strategy_actions import StrategyActionDependencies, build_strategy_actions_router
from app.routers.research_actions import ResearchActionDependencies, build_research_actions_router
from app.routers.ingestion_actions import IngestionActionDependencies, build_ingestion_actions_router
from app.board_rotation_read_model import latest_board_rotation_events
from app.replay_readiness import (
    P2_MIN_FULL_CROSS_SECTION_DAYS,
    P3_MIN_REPLAY_DAYS,
    P3_MIN_SIGNAL_EVENTS,
    replay_readiness_payload,
)
from app.strategy_pattern_read_model import latest_strategy_pattern_mining as read_latest_strategy_pattern_mining
from app.post_close_structures import (
    daily_base_structure as pure_daily_base_structure,
    post_close_forming_structure as pure_post_close_forming_structure,
    post_close_fresh_start_structure as pure_post_close_fresh_start_structure,
)
from app.database import pool_settings
from app.tushare_catalog import AUDITED_ADDITIONS_CATALOG, SUPPLIER_109_CATALOG, TUSHARE_CATALOG, catalog_counts
from app.free_market_providers import _request_with_retry, classify_announcement_title, cninfo_stock_param, eastmoney_secid, free_provider_status, parse_sina_quote_batch, tencent_symbol
from app.http_retry import retry_delay_seconds
from app.provider_rate_limits import provider_request_spacing_seconds, reserve_provider_rate_limit_slot
from app.akshare_provider import AkShareProviderError, _retry_call
from app.market_snapshots import snapshot_status, summarize_quotes
from app.tushare_official import HISTORICAL_MINUTE_APIS, REALTIME_MARKET_HOURS_APIS, default_probe_params
from app.tushare_providers import SUPER_GET_VERIFIED_APIS, SUPER_SDK_DELAYED_CONTEXT_APIS, SUPER_SDK_REALTIME_APIS, ProviderCallError, ProviderRateLimiter, _decode_rows, _filter_requested_realtime_rows, _normalize_ths_member_rows, _super_get_session, acquire_provider_request_slot, call_with_fallback, configure_provider_request_reserver, provider_candidates, provider_configs, provider_http_request, provider_request_reservation_status, provider_status, safe_error_detail, super_get_executor_status
from app.main import attach_intraday_volume_time_profile, daily_base_structure, intraday_limit_lift_pattern, intraday_signal_direction, intraday_signal_outcome_metrics, intraday_volume_time_profile, limit_board_count, merge_limit_pool_sources, persist_daily_bar_batch, persist_free_daily, persist_tushare_fetch_cancel, post_close_forming_structure, post_close_fresh_start_structure, post_close_limit_daily_features, strategy_pattern_review_score, watchlist_daily_factors
from app.main import StrategyDecisionRequest, run_strategy_decision
from app.main import intraday_board_curve_session, intraday_board_curve_session_async, realtime_market_session, realtime_market_session_async
from app.main import AnnouncementSyncRequest, sync_cninfo_announcements
from app.main import AkShareProbeRequest, akshare_probe, TushareCapabilityAuditRequest, audit_tushare_capabilities
from app.main import stock_study_free_fetch
from app.main import intraday_tencent_surge_context
from app.main import stock_study_fetch
from app.main import is_circuit_open_http_error, is_local_capacity_http_error
from app.main import fetch_tushare_catalog
from app.main import circuit_open_provider_keys_async
from app.main import FullMarketDailySyncRequest, MarketUniverseSyncRequest, TushareSyncRequest, sync_baostock, sync_full_market_daily, sync_market_universe, sync_tushare
from app.main import ConceptCandidateSyncRequest, ConceptMemberBackfillRequest, ConceptMemberSyncRequest, IntradayWatchlistRequest, PostCloseRefreshRequest, SectorFlowSyncRequest, attempt_intraday_alert_delivery, capture_intraday_board_flow_curve, capture_intraday_minute_sessions, delete_intraday_watchlist, deliver_board_rotation_alert, deliver_intraday_alert, hydrate_eastmoney_live_board_members, hydrate_watchlist_history, run_daily_strategy_summary, run_post_close_refresh, run_ths_concept_member_backfill_batch, sync_concept_limit_candidates, sync_eastmoney_board_members, sync_ths_concept_members, sync_ths_concept_signals, sync_ths_industry_moneyflow, sync_ths_sector_catalog, upsert_intraday_watchlist
from app.main import sync_all_ths_sector_catalogs
from app.main import GenerateRequest, run_daily_pipeline
from app.main import sync_runtime_provider_rate_limits


class ProviderHelperTests(unittest.TestCase):
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
        self.assertEqual(methods_by_path["/api/v1/stocks/{symbol}/study"], {"POST"})

    def test_async_sync_symbol_resolution_uses_database_executor(self):
        async def check() -> AsyncMock:
            blocking = AsyncMock(side_effect=[[{"symbol": "600519.SH"}], []])
            with patch.dict("os.environ", {"QUANT_UNIVERSE": ""}, clear=False), \
                 patch("app.main.run_database_blocking", new=blocking):
                symbols = await resolve_sync_symbols_async([])
            self.assertEqual(symbols, ["000300.SH", "600519.SH"])
            return blocking

        blocking = asyncio.run(check())
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["load_core"])

    def test_market_actions_router_has_explicit_write_contracts(self):
        action = AsyncMock(return_value={"status": "ok"})
        router = build_market_actions_router(MarketActionDependencies(
            import_bars=MagicMock(return_value={"imported": 0}), sync_universe=action,
            sync_full_daily=action, post_close_refresh=action, sync_announcements=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/market/bars/import", "/api/v1/market/universe/sync",
            "/api/v1/market/sync/full-daily", "/api/v1/market/post-close/refresh",
            "/api/v1/events/cninfo/sync",
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
            recompute_scorecards=action, recompute_outcomes=action,
            recompute_intraday_outcomes=action, generate_recommendations=action, daily_pipeline=action,
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/strategy/decisions/run", "/api/v1/strategy/reviews/run",
            "/api/v1/strategy/post-close/run", "/api/v1/strategy/pattern-mining/run",
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
        ))
        methods_by_path = {route.path: route.methods for route in router.routes}
        for path in (
            "/api/v1/analysis/jobs/{analysis_id}/run",
            "/api/v1/remote-archive/reports/import",
            "/api/v1/remote-archive/reports/reprocess",
            "/api/v1/remote-archive/messages/import",
            "/api/v1/remote-archive/messages/reprocess",
            "/api/v1/claim-review/{review_id}",
            "/api/v1/universes/members", "/api/v1/features/build",
            "/api/v1/factors/evaluate", "/api/v1/strategies/backtest",
            "/api/v1/operations/fetch-runs/reconcile-stale", "/api/v1/data-snapshots/build",
            "/api/v1/analyst-research/profiles/{analyst_id}",
        ):
            self.assertEqual(methods_by_path[path], {"PUT"} if path.endswith("/{analyst_id}") else {"POST"})

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
        self.assertEqual(connection.execute.call_count, 15)
        self.assertEqual(evidence["minute_profile"]["rows"], 0)
        self.assertIn("attempt_count<%s", connection.execute.call_args_list[10].args[0])

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
        )
        with patch("app.intraday_status_read_model.load_intraday_runtime_evidence", return_value=evidence):
            payload = read_intraday_services_status_payload(dependencies)

        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual([item["key"] for item in payload["items"]][-1], "primary_realtime")
        self.assertEqual(next(item for item in payload["items"] if item["key"] == "primary_realtime")["state"], "unavailable")
        database.transaction.assert_not_called()

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
        loops.fetchall.return_value = [{"lease_key": "background_loop:test", "expires_at": "later", "updated_at": "now"}]
        connection.execute.side_effect = [open_circuits, post_close, loops]
        pool_updates: list[dict[str, object]] = []
        circuit_updates: list[int] = []
        dependencies = HealthDependencies(
            database=database, post_close_lease_key="post-close", data_directory=lambda: Path("/tmp"),
            background_loop_lease_seconds=lambda: 120,
            resource_status=lambda _: {"state": "healthy"}, public_http_client_status=lambda: {"active": True},
            alert_http_client_status=lambda: {"active": True}, provider_http_client_status=lambda: {"active": True},
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
        )
        payload = read_health_payload(dependencies)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["runtime_leases"]["background_loop_lease_seconds"], 120)
        self.assertTrue(payload["provider_rate_limits"]["shared_database_reservation"])
        self.assertEqual(payload["runtime_leases"]["background_loops"][0]["lease_key"], "background_loop:test")
        self.assertEqual(pool_updates, [{"pool_size": 2, "available": 1, "waiting": 0}])
        self.assertEqual(circuit_updates, [2])

        database.ping.side_effect = RuntimeError("database down")
        with self.assertRaises(DatabaseUnavailableError):
            read_health_payload(dependencies)

    def test_http_clients_are_reused_only_inside_the_service_lifecycle(self):
        async def check() -> tuple[bool, bool, bool, bool, bool, int]:
            await close_http_clients()
            await start_http_clients()
            async with public_http_client() as first, public_http_client() as second:
                reused = first is second
            async with alert_http_client() as first_alert, alert_http_client() as second_alert:
                alert_reused = first_alert is second_alert
            async with provider_http_client("tushare_super_sdk", "http://proxy.example:8080") as first_provider, \
                    provider_http_client("tushare_super_sdk", "http://proxy.example:8080") as second_provider:
                provider_reused = first_provider is second_provider
            active_before_close = bool(public_http_client_status()["lifecycle_owned"])
            alert_active_before_close = bool(alert_http_client_status()["lifecycle_owned"])
            active_provider_pools = int(provider_http_client_status()["active_provider_pools"])
            await close_http_clients()
            active_after_close = bool(public_http_client_status()["lifecycle_owned"])
            return reused, alert_reused, provider_reused, active_before_close, alert_active_before_close, active_provider_pools, active_after_close

        reused, alert_reused, provider_reused, active_before_close, alert_active_before_close, active_provider_pools, active_after_close = asyncio.run(check())
        self.assertTrue(reused)
        self.assertTrue(alert_reused)
        self.assertTrue(provider_reused)
        self.assertTrue(active_before_close)
        self.assertTrue(alert_active_before_close)
        self.assertEqual(active_provider_pools, 1)
        self.assertFalse(active_after_close)
        self.assertFalse(alert_http_client_status()["lifecycle_owned"])
        self.assertEqual(provider_http_client_status()["active_provider_pools"], 0)

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

        ready = replay_readiness_payload({
            "full_cross_section_days": P2_MIN_FULL_CROSS_SECTION_DAYS,
            "offline_minute_trading_days": P3_MIN_REPLAY_DAYS,
            "offline_minute_symbols": 10, "offline_minute_bars": 10_000,
            "completed_offline_imports": 1, "confirmed_signal_events": P3_MIN_SIGNAL_EVENTS,
            "matured_signal_events": P3_MIN_SIGNAL_EVENTS,
        })
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["p2_data_foundation_ready"])
        self.assertTrue(ready["p3_strategy_validation_ready"])

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
        self.assertEqual(connection.execute.call_args_list[0].args[1], (5000,))
        self.assertEqual(batch_calls, [[(observed_at, "600000.SH")]])
        self.assertEqual(payload["items"][0]["attribution"]["stage"], "generic")
        self.assertEqual(payload["attribution_window_limit"], 5000)
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
            }},
            {"label": "浦发银行"}, {"name": "浦发银行"}, {"time": "2026-08-11 10:00:00", "close": 12.3},
            decision_card_url="https://research.example/?section=research&tab=stock-study&symbol=600000.SH",
        )
        self.assertIn("EAC 首突破", text)
        self.assertIn("秒级价格交叉确认", text)
        self.assertIn("信号观测时间（上海）", text)
        self.assertIn("决策卡（已保存证据）", text)
        self.assertIn("仅为人工复核提醒", text)

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

    def test_provider_failure_recording_redacts_credentials(self):
        connection = MagicMock()
        record_provider_failure(connection, "test", "daily", "Authorization: credential-value")
        parameters = connection.execute.call_args.args[1]
        self.assertNotIn("credential-value", parameters[-1])

    def test_intraday_outcome_decomposition_is_json_safe_before_persistence(self):
        from app.main import strategy_json_safe
        decomposition = a_share_return_decomposition(
            Decimal("10"), 1, Decimal("10.5"), Decimal("10.2"), Decimal("10.8"),
        )
        persisted = strategy_json_safe({"return_decomposition": decomposition})
        self.assertIsInstance(persisted["return_decomposition"]["overnight"], str)

    def test_capability_circuit_lookup_returns_only_open_entries(self):
        async def check() -> set[str]:
            with patch("app.main.run_database_blocking", new=AsyncMock(return_value=[{"capability": "intraday_board_flow_concept"}])):
                return await open_provider_capabilities(
                    "eastmoney_free", ["intraday_board_flow_concept", "intraday_board_flow_industry"],
                )
        self.assertEqual(asyncio.run(check()), {"intraday_board_flow_concept"})

    def test_generic_provider_circuit_lookup_uses_database_executor(self):
        providers = [MagicMock(key="tushare_primary"), MagicMock(key="tushare_super_sdk")]

        async def check() -> set[str]:
            with patch("app.main.run_database_blocking", new=AsyncMock(return_value=[{"provider_key": "tushare_super_sdk"}])):
                return await circuit_open_provider_keys_async("daily", providers)

        self.assertEqual(asyncio.run(check()), {"tushare_super_sdk"})

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
            blocking = AsyncMock(side_effect=[None, []])
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
            "persist_snapshot", "evaluate_intraday_board_rotation_events",
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

    def test_ths_concept_backfill_uses_database_executor_for_progress(self):
        completed = {"status": "completed", "total_concepts": 3, "member_results": []}

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(side_effect=[{"rows": 1}, {"done": 2, "failed": 1}])
            with patch("app.main.sync_ths_concept_members", new=AsyncMock(return_value=completed)), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await run_ths_concept_member_backfill_batch(ConceptMemberBackfillRequest(trade_date=date(2026, 8, 11), refresh_flow_catalog=False))
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["progress"], {"completed_or_empty": 2, "failed": 1, "remaining": 1})
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["load_existing", "load_progress"])

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

            def fetchone(self):
                return {"sample_days": 8, "median_volume": 200}

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
            blocking = AsyncMock(side_effect=[uuid.uuid4(), None])
            with patch("app.main.post_feishu_alert_text", new=AsyncMock(return_value={"status": "disabled", "reason": "not configured"})), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await deliver_intraday_alert(uuid.uuid4(), "测试")
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "disabled")
        self.assertEqual([call.args[0].__name__ for call in blocking.await_args_list], ["create_pending_delivery", "persist_delivery_attempt"])

    def test_intraday_alert_retry_keeps_failed_message_outbox_bounded(self):
        from app.main import retry_pending_intraday_alerts

        due = [{"delivery_id": uuid.uuid4(), "signal_event_id": uuid.uuid4(), "message_text": "未送达提醒"}]

        async def check() -> tuple[dict[str, int], AsyncMock]:
            blocking = AsyncMock(return_value=due)
            with patch("app.main.run_database_blocking", new=blocking), \
                 patch("app.main.attempt_intraday_alert_delivery", new=AsyncMock(return_value={"status": "sent"})):
                return await retry_pending_intraday_alerts(limit=99), blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result, {"loaded": 1, "sent": 1, "failed": 0, "disabled": 0})
        self.assertEqual(blocking.await_args.args[0].__name__, "load_due")

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
            blocking = AsyncMock(return_value=2)
            with patch("app.main.run_database_blocking", new=blocking), \
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
                {"status": "ready"}, {"outcomes": 1}, {"scorecards": 1}, {"recommendations": 1},
            ])
            with patch("app.main.sync_tushare", new=AsyncMock(return_value={"status": "completed"})), \
                 patch("app.main.sync_tushare_daily_core", new=AsyncMock(return_value={"status": "completed"})), \
                 patch("app.main.run_database_blocking", new=blocking):
                result = await run_daily_pipeline(GenerateRequest())
            self.assertEqual(result["status"], "completed")
            return blocking

        blocking = asyncio.run(check())
        self.assertEqual(
            [call.args[0].__name__ for call in blocking.await_args_list],
            ["build_snapshot", "recompute_outcomes", "recompute_scorecards", "generate_recommendations"],
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
            with patch("app.main.run_database_blocking", new=AsyncMock(side_effect=[None, {"is_open": True}])):
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

    def test_async_realtime_session_gate_uses_database_executor_and_fails_closed(self):
        during_continuous_auction = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)

        async def check() -> tuple[tuple[bool, str], tuple[bool, str]]:
            with patch("app.main.run_database_blocking", new=AsyncMock(side_effect=[None, {"is_open": True}])):
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
            saturated = ExecutorSaturatedError("database blocking executor is saturated")
            with patch("app.main.run_database_blocking", new=AsyncMock(side_effect=[saturated, saturated, saturated])):
                return (
                    await sse_calendar_open_async(date(2026, 8, 11)),
                    await realtime_market_session_async(now=during_continuous_auction),
                    await intraday_board_curve_session_async(now=during_board_observation),
                )

        calendar_open, realtime, board = asyncio.run(check())
        self.assertFalse(calendar_open)
        self.assertFalse(realtime[0])
        self.assertIn("local calendar capacity", realtime[1])
        self.assertFalse(board[0])
        self.assertIn("local calendar capacity", board[1])

    def test_intraday_sector_report_runs_local_membership_join_in_database_executor(self):
        local_report = [{"taxonomy_key": "eastmoney_concept", "sector_key": "BK001", "label": "测试概念",
                         "net_inflow": 123.0, "change_pct": 1.2, "mapped_members": 1,
                         "quoted_members": 1, "top_stocks": []}]

        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock(return_value=(local_report, {"concept": {"flow_boards": 1, "boards_with_members": 1}}, [], [], []))
            with patch("app.main.run_akshare_blocking", new=AsyncMock(side_effect=[
                [{"板块名称": "测试概念", "流入资金": 200, "流出资金": 77}],
                [{"code": "sz000001", "name": "测试股"}],
            ])), patch("app.main.run_database_blocking", new=blocking):
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

            task = asyncio.create_task(supervise_loop("test_loop", loop, restart_delay_seconds=0.01))
            await asyncio.wait_for(second_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return starts

        with patch("builtins.print"):
            self.assertEqual(asyncio.run(check()), 2)

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

    def test_runtime_resource_thresholds_are_bounded_and_explain_degradation(self):
        self.assertEqual(bounded_min_free_bytes("invalid"), 1024 ** 3)
        self.assertEqual(bounded_memory_ratio("2"), 0.98)
        state, reasons = runtime_resource_state(
            disk_free_bytes=10, min_free_bytes=100, rss_bytes=90, memory_limit_bytes=100, max_memory_ratio=0.85,
        )
        self.assertEqual(state, "degraded")
        self.assertEqual(len(reasons), 2)

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
            "main.py", "tushare_providers.py",
        })
        self.assertNotIn("AsyncClient(", (app_dir / "free_market_providers.py").read_text(encoding="utf-8"))
        self.assertIn("public_http_client()", (app_dir / "free_market_providers.py").read_text(encoding="utf-8"))
        self.assertNotIn("AsyncClient(", (app_dir / "tushare_providers.py").read_text(encoding="utf-8"))
        self.assertIn("provider_http_client(", (app_dir / "tushare_providers.py").read_text(encoding="utf-8"))
        self.assertIn("alert_http_client()", (app_dir / "alert_transport.py").read_text(encoding="utf-8"))

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
        self.assertEqual(catalog_counts()["offline_files_only"], 6)
        self.assertIn("stk_auction", TUSHARE_CATALOG)
        self.assertIn("stk_auction_o", TUSHARE_CATALOG)
        self.assertIn("stk_auction_c", TUSHARE_CATALOG)
        self.assertIn("rt_min", TUSHARE_CATALOG)
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
                with patch("app.tushare_providers.request_limiter.acquire", new=AsyncMock(side_effect=lambda *_: sequence.append("local"))):
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
        with patch.dict("os.environ", {"INTRADAY_FAST_QUOTE_RETENTION_DAYS": "7"}):
            self.assertEqual(intraday_fast_quote_retention_days(), 7)
        with patch.dict("os.environ", {"INTRADAY_BOARD_ROTATION_RETENTION_DAYS": "60"}):
            self.assertEqual(intraday_board_rotation_retention_days(), 60)
        with patch.dict("os.environ", {"INTRADAY_BOARD_ROTATION_RETENTION_DAYS": "invalid"}):
            self.assertEqual(intraday_board_rotation_retention_days(), 60)

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

    def test_intraday_watchlist_rules_require_confirmation_except_hard_stop(self):
        scan = IntradayScanRequest(symbols=["600176.sh", "600176.SH"], realtime_validation_limit=2)
        self.assertEqual(scan.symbols, ["600176.SH"])
        expanded_scan = IntradayScanRequest(symbols=[f"{600000 + index:06d}.SH" for index in range(21)])
        self.assertEqual(len(expanded_scan.symbols), 21)
        quote = intraday_quote_from_tencent({"code": "sh600176", "name": "中国巨石", "zxj": "42.10", "zdf": "2.1", "lb": "2.3", "hsl": "4.2", "zljlr": "123.0"})
        self.assertEqual(quote["symbol"], "600176.SH")
        entry_watch = {"symbol": "600176.SH", "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
        entry = intraday_signal_rules(entry_watch, quote, {"price": 42.00})
        self.assertEqual(entry[0]["signal_type"], "entry")
        self.assertFalse(entry[0]["hard"])
        position_watch = {"symbol": "600176.SH", "entry_price": 42.50, "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True, "hard_stop": 42.20}
        exit_signal = intraday_signal_rules(position_watch, quote, {"price": 42.00})
        self.assertEqual(exit_signal[0]["signal_type"], "exit")
        self.assertTrue(exit_signal[0]["hard"])

    def test_cross_sectional_flow_extremes_are_unit_independent(self):
        quotes = {
            "000001.SZ": {"main_net_inflow": -900, "volume_ratio": 2.0},
            "000002.SZ": {"main_net_inflow": 0, "volume_ratio": 1.0},
            "000003.SZ": {"main_net_inflow": 900, "volume_ratio": 2.0},
        }
        annotate_intraday_flow_percentiles(quotes)
        self.assertEqual(quotes["000001.SZ"]["main_flow_percentile"], 0.0)
        self.assertEqual(quotes["000003.SZ"]["main_flow_percentile"], 1.0)

    def test_batched_watch_quote_refreshes_price_without_inventing_flow(self):
        quotes = {"000001.SZ": {"symbol": "000001.SZ", "price": 10.0, "pct_change": 0.0,
                                 "main_net_inflow": 123.0, "main_flow_percentile": 0.9, "raw": {}}}
        merged = merge_intraday_watch_quote_prices(
            quotes, [{"ts_code": "000001.SZ", "name": "平安银行", "price": 10.2, "pre_close": 10.0}],
        )
        self.assertEqual(merged["000001.SZ"]["price"], 10.2)
        self.assertEqual(merged["000001.SZ"]["main_net_inflow"], 123.0)
        self.assertEqual(merged["000001.SZ"]["price_source"], "tencent_batched_watch_quote")

    def test_sina_watch_fallback_keeps_flow_fields_absent(self):
        merged = merge_intraday_sina_watch_quotes({}, [{"ts_code": "000001.SZ", "name": "平安银行", "close": 10.2, "pre_close": 10.0}])
        self.assertEqual(merged["000001.SZ"]["price"], 10.2)
        self.assertNotIn("main_net_inflow", merged["000001.SZ"])
        self.assertEqual(merged["000001.SZ"]["price_source"], "sina_batched_watch_quote")
        watch = {"symbol": "000001.SZ", "entry_price": 10, "available_quantity": 0, "alert_on_entry": False, "alert_on_exit": True}
        quote = {"price": 9.8, "pct_change": -2, "volume_ratio": 2, "turnover_rate": 5, "main_net_inflow": -900, "main_flow_percentile": 0.0}
        self.assertEqual(intraday_signal_rules(watch, quote, {"price": 10})[0]["signal_key"], "000001.SZ:reduce:extreme_flow_sell")
        extension_watch = {"symbol": "002842.SZ", "entry_price": None, "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
        extension_quote = {"price": 40.89, "pct_change": 6.54, "volume_ratio": 1.45, "turnover_rate": 20.21, "main_net_inflow": 6850, "main_flow_percentile": 0.97108}
        self.assertEqual(intraday_signal_rules(extension_watch, extension_quote, {"price": 40.80})[0]["signal_key"], "002842.SZ:watch:price_extension")

    def test_ths_concept_top_stocks_requires_exact_concept_code_membership(self):
        flows = [{"sector_key": "885001.TI", "label": "精确概念", "net_amount": 321, "change_pct": 2.1, "trading_date": date(2026, 8, 10)}]
        members = [{"sector_key": "885001.TI", "symbol": "000001.SZ"}, {"sector_key": "885001.TI", "symbol": "000002.SZ"},
                   {"sector_key": "885999.TI", "symbol": "000003.SZ"}]
        quotes = {
            "000001.SZ": {"symbol": "000001.SZ", "main_net_inflow": 50, "turnover": 100},
            "000002.SZ": {"symbol": "000002.SZ", "main_net_inflow": 150, "turnover": 50},
            "000003.SZ": {"symbol": "000003.SZ", "main_net_inflow": 999, "turnover": 999},
        }
        items, coverage = ths_concept_top_stocks(flows, members, quotes, 10)
        self.assertEqual(items[0]["taxonomy_key"], "ths_concept_flow")
        self.assertEqual([stock["symbol"] for stock in items[0]["top_stocks"]], ["000002.SZ", "000001.SZ"])
        self.assertEqual(items[0]["mapped_members"], 2)
        self.assertEqual(coverage, {"flow_boards": 1, "boards_with_members": 1, "quoted_members": 2})

    def test_board_stock_mining_requires_complete_exact_membership_and_keeps_both_directions(self):
        exact_inflow = {
            "taxonomy_key": "eastmoney_industry", "sector_key": "gold", "label": "贵金属",
            "net_inflow": 500, "change_pct": 1.2, "mapped_members": 2, "quoted_members": 2,
            "member_quotes": [
                {"symbol": "000001.SZ", "name": "流入龙头", "main_net_inflow": 100, "volume_ratio": 2.1, "turnover_rate": 4.0, "pct_change": 3.0},
                {"symbol": "000002.SZ", "name": "量能不足", "main_net_inflow": 60, "volume_ratio": 1.1, "turnover_rate": 3.0, "pct_change": 1.0},
            ],
        }
        exact_outflow = {
            "taxonomy_key": "eastmoney_industry", "sector_key": "breed", "label": "养殖业",
            "net_inflow": -400, "change_pct": -1.1, "mapped_members": 1, "quoted_members": 1,
            "member_quotes": [{"symbol": "000003.SZ", "name": "流出风险", "main_net_inflow": -90, "volume_ratio": 2.0, "turnover_rate": 5.0, "pct_change": -2.5}],
        }
        partial = {
            "taxonomy_key": "eastmoney_concept", "sector_key": "unmapped", "label": "不得猜归属",
            "net_inflow": 999, "mapped_members": 2, "quoted_members": 1,
            "member_quotes": [{"symbol": "000004.SZ", "main_net_inflow": 999, "volume_ratio": 9, "turnover_rate": 9, "pct_change": 9}],
        }
        candidates, coverage, summary = board_stock_mining_candidates([exact_inflow, exact_outflow, partial])
        self.assertEqual([(item["direction"], item["symbol"]) for item in candidates], [("inflow", "000001.SZ"), ("outflow", "000003.SZ")])
        self.assertEqual(candidates[0]["setup_key"], "board_inflow_leader")
        self.assertEqual(candidates[1]["setup_key"], "board_outflow_risk")
        self.assertEqual(coverage["exact_complete_boards"], 2)
        self.assertEqual(coverage["partial_or_unmapped_boards_skipped"], 1)
        self.assertEqual(summary["returned"], 2)

    def test_limit_linkage_mining_requires_exact_relation_flow_and_activity(self):
        relations = [
            {"symbol": "000001.SZ", "shared_concepts": 2, "concept_labels": ["低空经济"],
             "leader_symbols": ["000002.SZ"], "leader_names": ["涨停龙头"]},
            {"symbol": "000003.SZ", "shared_concepts": 1, "concept_labels": ["低空经济"],
             "leader_symbols": ["000002.SZ"], "leader_names": ["涨停龙头"]},
        ]
        quotes = {
            "000001.SZ": {"symbol": "000001.SZ", "name": "联动候选", "main_net_inflow": 80,
                            "volume_ratio": 2.0, "turnover_rate": 3.0, "pct_change": 4.2},
            "000003.SZ": {"symbol": "000003.SZ", "name": "加速末段排除", "main_net_inflow": 200,
                            "volume_ratio": 4.0, "turnover_rate": 8.0, "pct_change": 7.2},
        }
        candidates, summary = limit_linkage_candidates(relations, quotes)
        self.assertEqual([item["symbol"] for item in candidates], ["000001.SZ"])
        self.assertEqual(candidates[0]["leader_symbols"], ["000002.SZ"])
        self.assertEqual(candidates[0]["risk_flags"], ["leader_linkage_research_only", "requires_minute_confirmation"])
        self.assertEqual(summary["anchors"], 1)

    def test_order_book_observation_uses_depth_and_nonnegative_cumulative_deltas(self):
        previous = {
            "bids": [{"price": 10.00, "size": 100}, {"price": 9.99, "size": 50}],
            "asks": [{"price": 10.01, "size": 120}, {"price": 10.02, "size": 60}],
            "cumulative_volume_lot": 1000, "cumulative_amount": 1_000_000,
            "outer_volume_lot": 600, "inner_volume_lot": 400,
        }
        current = {
            "bids": [{"price": 10.01, "size": 180}, {"price": 10.00, "size": 80}],
            "asks": [{"price": 10.02, "size": 90}, {"price": 10.03, "size": 50}],
            "cumulative_volume_lot": 1010, "cumulative_amount": 1_010_500,
            "outer_volume_lot": 620, "inner_volume_lot": 405,
        }
        observed = order_book_observation(current, previous)
        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["delta_status"], "ready")
        self.assertGreater(observed["qi1"], 0)
        self.assertGreater(observed["ofi_best_level"], 0)
        self.assertEqual(observed["cumulative_volume_delta_lot"], 10.0)
        self.assertEqual(observed["interval_vwap"], 10.5)

    def test_tencent_order_book_decoder_reads_cumulative_amount_from_field_35(self):
        values = [""] * 36
        values[1], values[3], values[4] = "样本", "10.20", "10.00"
        values[6], values[7], values[8] = "1000", "610", "390"
        for level in range(5):
            values[9 + level * 2], values[10 + level * 2] = f"{10.19 - level * 0.01:.2f}", str(100 - level)
            values[19 + level * 2], values[20 + level * 2] = f"{10.21 + level * 0.01:.2f}", str(90 - level)
        values[30], values[35] = "20260812130000", "10.20/1000/1020000"
        row = _tencent_order_book_row("000001.SZ", values)
        self.assertIsNotNone(row)
        self.assertEqual(row["cumulative_amount"], 1_020_000.0)
        self.assertEqual(row["bids"][0], {"price": 10.19, "size": 100.0})

    def test_tencent_order_book_decoder_preserves_a_limit_up_seal_with_empty_asks(self):
        values = [""] * 36
        values[1], values[3], values[4] = "涨停样本", "10.99", "9.99"
        values[6], values[7], values[8] = "1000", "610", "390"
        values[9], values[10] = "10.99", "557769"
        for level in range(1, 5):
            values[9 + level * 2], values[10 + level * 2] = "0.00", "0"
        for level in range(5):
            values[19 + level * 2], values[20 + level * 2] = "0.00", "0"
        values[30], values[35] = "20260812130000", "10.99/1000/1099000"
        row = _tencent_order_book_row("000001.SZ", values)
        self.assertIsNotNone(row)
        self.assertTrue(row["one_sided_book"])
        self.assertEqual(row["book_side"], "bid_only")
        self.assertEqual(row["seal_volume_lot"], 557769.0)
        observed = order_book_observation(row)
        self.assertEqual(observed["qi1"], 1.0)
        self.assertEqual(observed["qi5"], 1.0)
        self.assertEqual(observed["seal_volume_lot"], 557769.0)

    def test_order_book_observation_never_turns_counter_reset_into_negative_turnover(self):
        observed = order_book_observation(
            {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}],
             "cumulative_volume_lot": 10, "cumulative_amount": 1000, "outer_volume_lot": 4, "inner_volume_lot": 6},
            {"bids": [{"price": 10, "size": 9}], "asks": [{"price": 10.01, "size": 11}],
             "cumulative_volume_lot": 100, "cumulative_amount": 10_000, "outer_volume_lot": 50, "inner_volume_lot": 50},
        )
        self.assertEqual(observed["cumulative_volume_delta_lot"], 0.0)
        self.assertEqual(observed["cumulative_amount_delta"], 0.0)
        self.assertIsNone(observed["interval_vwap"])

    def test_order_book_levels_preserve_empty_intermediate_positions_for_qi_weights(self):
        observed = order_book_observation({
            "bids": [{"price": 10, "size": 100}, {"price": 0, "size": 0}, {"price": 9.98, "size": 100}],
            "asks": [{"price": 10.01, "size": 100}, {"price": 10.02, "size": 100}, {"price": 10.03, "size": 100}],
        })
        # If the invalid bid2 were compacted out, bid3 would wrongly receive
        # the bid2 weight (0.6065), rather than its true bid3 weight (0.3679).
        self.assertAlmostEqual(observed["bid_depth_lot"], 136.79, places=2)

    def test_order_book_observation_does_not_fabricate_zero_vwap_when_amount_did_not_move(self):
        observed = order_book_observation(
            {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}],
             "cumulative_volume_lot": 11, "cumulative_amount": 1000},
            {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}],
             "cumulative_volume_lot": 10, "cumulative_amount": 1000},
        )
        self.assertEqual(observed["cumulative_volume_delta_lot"], 1.0)
        self.assertEqual(observed["cumulative_amount_delta"], 0.0)
        self.assertIsNone(observed["interval_vwap"])

    def test_order_book_aggregate_uses_windows_not_a_single_noisy_frame(self):
        at = datetime(2026, 8, 12, 5, 5, tzinfo=timezone.utc)
        rows = [
            {"observed_at": at - timedelta(seconds=offset), "raw": {"order_book_features": {"ofi_best_level": value}}}
            for offset, value in ((3, 2), (6, 3), (9, -1), (70, 9), (310, 99))
        ]
        aggregate = aggregate_order_book_observations(rows, at)
        self.assertEqual(aggregate["ofi_30s"], 4.0)
        self.assertEqual(aggregate["ofi_30s_sample_count"], 3)
        self.assertEqual(aggregate["ofi_1m"], 4.0)
        self.assertEqual(aggregate["ofi_5m"], 13.0)

    def test_smart_money_q_uses_the_same_rolling_window_vwap_and_volume_share(self):
        rows = []
        for index in range(30):
            close = 10.0 + index * 0.01
            volume = 100 if index < 29 else 1000
            rows.append({"time": f"10{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.0})
        features = intraday_minute_features(rows)
        self.assertIsNotNone(features)
        self.assertGreaterEqual(features["smart_money_selected_volume_share_30m"], 0.2)
        self.assertNotEqual(features["smart_money_window_vwap_30m"], 9.0)

    def test_outcome_decomposition_keeps_overnight_separate_from_intraday_return(self):
        result = a_share_return_decomposition(Decimal("10"), 1, Decimal("10.5"), Decimal("10.2"), Decimal("10.8"))
        self.assertEqual(result["trigger_to_close"], Decimal("0.05"))
        self.assertEqual(result["overnight"], Decimal("-0.0285714285714285714285714286"))
        self.assertAlmostEqual(float(result["next_day_intraday"] or 0), 0.0588235294117647059, places=12)
        self.assertEqual(result["trigger_to_next_close"], Decimal("0.08"))

    def test_intraday_attribution_labels_microstructure_as_observational(self):
        attribution = intraday_signal_attribution(
            "000001.SZ:watch:test", "watch", {},
            {"tencent_order_book": {"status": "observed", "latest_features": {"status": "observed", "delta_status": "ready", "qi5": 0.4},
                                      "ofi_30s": 3, "ofi_30s_sample_count": 3},
             "tencent_minute": {"price_log_volume_corr_30m": -0.4, "smart_money_q_30m": 0.99}},
        )
        self.assertEqual(attribution["microstructure_state"], "observed_bid_heavy_positive_ofi_30s")
        self.assertEqual(attribution["ofi_attribution_window"], "30s")
        self.assertEqual(attribution["price_volume_state"], "negative_corr")
        self.assertEqual(attribution["smart_money_state"], "below_session_vwap")

    def test_green_reclaim_research_rule_requires_price_volume_vwap_and_flow_confirmation(self):
        rows = []
        for index in range(25):
            close = 10.0
            if index == 4:
                close = 9.8
            if index == 22:
                close = 10.0
            if index == 23:
                close = 10.1
            if index == 24:
                close = 10.2
            rows.append({"time": f"10{index:02d}", "close": close, "volume_lot": 4 if index == 24 else 1,
                         "amount": close * (400 if index == 24 else 100), "vwap": 9.95})
        feature = intraday_minute_features(rows)
        self.assertIsNotNone(feature)
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"reversal_research": {"enabled": True, "label": "research"}}}
        quote = {"price": 10.2, "pct_change": 2.0, "volume_ratio": 2.0, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.95}
        previous = {"price": 9.98, "pct_change": -0.2}
        signals = intraday_signal_rules(watch, quote, previous, None, feature, {"available_peer_count": 0})
        self.assertEqual(signals[0]["signal_key"], "000001.SZ:watch:green_reclaim_research_v1")

    def test_upside_breakout_research_requires_causal_high_volume_vwap_and_flow(self):
        rows = []
        prices = [10.0] * 20 + [10.05, 10.10, 10.25, 10.40]
        for index, close in enumerate(prices):
            volume = 4 if index == len(prices) - 1 else 1
            rows.append({"time": f"10{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.99})
        feature = intraday_minute_features(rows)
        self.assertGreater(feature["breakout_above_prior_high_pct"], 0)
        self.assertGreaterEqual(feature["session_range_position"], 0.99)
        feature["time_bucket_volume_profile"] = {"status": "ready", "sample_days": 8,
                                                  "median_volume": 1, "volume_surprise": 4}
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"upside_research": {"enabled": True, "label": "breakout"}}}
        quote = {"price": 10.4, "pct_change": 4.0, "volume_ratio": 1.8, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        daily = {"status": "completed", "ma_trend": "bullish"}
        signals = intraday_signal_rules(watch, quote, {"price": 10.25}, daily, feature, {"available_peer_count": 0})
        self.assertEqual(signals[0]["signal_key"], "000001.SZ:watch:upside_breakout_eac_v3")
        self.assertEqual(signals[0]["conditions"]["upside_research_assessment"]["status"], "candidate")
        quote["pct_change"] = 6.6
        self.assertFalse(any(item["signal_key"] == "000001.SZ:watch:upside_breakout_eac_v3"
                             for item in intraday_signal_rules(watch, quote, {"price": 10.25}, daily, feature, {})))

    def test_eac_marks_extreme_minute_volume_as_attention_not_candidate(self):
        rows = []
        prices = [10.0] * 20 + [10.05, 10.10, 10.25, 10.40]
        for index, close in enumerate(prices):
            volume = 30 if index == len(prices) - 1 else 1
            rows.append({"time": f"10:{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.99})
        feature = intraday_minute_features(rows)
        feature["time_bucket_volume_profile"] = {"status": "ready", "sample_days": 8,
                                                  "median_volume": 1, "volume_surprise": 30}
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"upside_research": {"enabled": True}}}
        quote = {"price": 10.4, "pct_change": 4.0, "volume_ratio": 1.8, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        signals = intraday_signal_rules(watch, quote, {"price": 10.25}, {"ma_trend": "bullish"}, feature, {})
        self.assertEqual(signals[0]["conditions"]["eac_state"], "attention_only")
        self.assertIn("relative_volume_outlier_requires_acceptance", signals[0]["risk_flags"])

    def test_eac_without_same_clock_history_is_attention_only(self):
        rows = []
        prices = [10.0] * 20 + [10.05, 10.10, 10.25, 10.40]
        for index, close in enumerate(prices):
            volume = 4 if index == len(prices) - 1 else 1
            rows.append({"time": f"10:{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.99})
        feature = intraday_minute_features(rows)
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"upside_research": {"enabled": True}}}
        quote = {"price": 10.4, "pct_change": 4.0, "volume_ratio": 1.8, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        signals = intraday_signal_rules(watch, quote, {"price": 10.25}, {"ma_trend": "bullish"}, feature, {})
        assessment = signals[0]["conditions"]["upside_research_assessment"]
        self.assertEqual(assessment["status"], "attention_only")
        self.assertIn("time_bucket_volume_baseline_insufficient", signals[0]["risk_flags"])

    def test_eac_acceptance_requires_time_price_vwap_and_range_retention(self):
        first_at = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        first_conditions = {"price": 10.40, "upside_research_assessment": {"status": "candidate"}}
        quote = {"price": 10.38, "pct_change": 3.8, "main_flow_percentile": 0.9}
        minute = {"above_vwap_pct": 1.2, "session_range_position": 0.88}
        accepted = intraday_eac_acceptance_assessment(
            first_conditions, first_observed_at=first_at,
            observed_at=first_at + __import__("datetime").timedelta(seconds=40),
            quote=quote, previous_quote={"price": 10.39}, minute_features=minute,
            peer_context={"available_peer_count": 0},
        )
        self.assertEqual(accepted["status"], "candidate")
        early = intraday_eac_acceptance_assessment(
            first_conditions, first_observed_at=first_at,
            observed_at=first_at + __import__("datetime").timedelta(seconds=10),
            quote=quote, previous_quote={"price": 10.39}, minute_features=minute,
            peer_context={"available_peer_count": 0},
        )
        self.assertEqual(early["status"], "not_confirmed")
        attention = intraday_eac_acceptance_assessment(
            {"price": 10.40, "upside_research_assessment": {"status": "attention_only"}},
            first_observed_at=first_at, observed_at=first_at + __import__("datetime").timedelta(seconds=40),
            quote=quote, previous_quote={"price": 10.39}, minute_features=minute,
            peer_context={"available_peer_count": 0},
        )
        self.assertEqual(attention["status"], "attention_only")

    def test_multi_index_regime_calls_a_rebound_corrective_without_inferring_wave_count(self):
        rows = []
        prices = [100, 98, 96, 94, 92, 90, 87, 84, 82, 80,
                  81, 82, 83, 84, 85, 86, 87, 88, 89, 90]
        for symbol in ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ"):
            for index, close in enumerate(prices, start=1):
                rows.append({"symbol": symbol, "trading_date": date(2026, 7, index),
                             "high": close + 1, "low": close - 1, "close": close, "volume": 100})
        regime = strategy_index_regime(rows)
        self.assertEqual(regime["state"], "corrective_rebound")
        self.assertEqual(regime["index_count"], 4)
        self.assertEqual(regime["interpretation"], "B-wave is an analyst scenario label only")

    def test_intraday_attribution_separates_acceptance_market_and_sector_linkage(self):
        attribution = intraday_signal_attribution(
            "000001.SZ:entry:upside_acceptance_eac_v4", "entry",
            {"setup": "eac_acceptance_confirmed", "eac_acceptance_assessment": {"status": "candidate"}},
            {"peer_context": {"available_peer_count": 3, "confirming_peer_count": 2}},
            {"market_state": "rotation_defensive", "board_snapshot_age_seconds": 45,
             "symbol_board_matches": [{"label": "小金属", "net_inflow": 10}]},
        )
        self.assertEqual(attribution["model_version"], "eac-v4")
        self.assertEqual(attribution["stage"], "acceptance")
        self.assertEqual(attribution["market_state"], "rotation_defensive")
        self.assertEqual(attribution["sector_linkage"], "peer_and_board_top10_confirmed")
        self.assertEqual(attribution["volume_baseline"], "ready")

    def test_intraday_point_in_time_context_batch_uses_one_board_report_query(self):
        connection = MagicMock()
        first_report = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
        second_report = datetime(2026, 8, 10, 1, 5, tzinfo=timezone.utc)
        connection.execute.return_value.fetchall.return_value = [
            {"board_report_id": "first", "observed_at": first_report, "payload": {"items": []}},
            {"board_report_id": "second", "observed_at": second_report, "payload": {"items": []}},
        ]
        first_signal = datetime(2026, 8, 10, 1, 3, tzinfo=timezone.utc)
        second_signal = datetime(2026, 8, 10, 1, 7, tzinfo=timezone.utc)
        contexts = intraday_point_in_time_market_context_batch(
            connection, [(first_signal, "600000.SH"), (second_signal, "600000.SH"), (second_signal, "000001.SZ")],
        )
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(contexts[(first_signal, "600000.SH")]["board_report_id"], "first")
        self.assertEqual(contexts[(second_signal, "600000.SH")]["board_report_id"], "second")
        self.assertEqual(contexts[(second_signal, "000001.SZ")]["board_report_id"], "second")

    def test_intraday_attribution_summary_keeps_small_cohorts_descriptive(self):
        observed = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        base = {"horizon_key": "5m", "status": "matured", "observed_at": observed,
                "maximum_favorable_excursion": 0.02, "maximum_adverse_excursion": -0.01,
                "attribution": {"model_version": "eac-v4", "stage": "acceptance",
                                "market_state": "rotation_defensive", "sector_linkage": "peer_confirmed",
                                "volume_baseline": "ready"}}
        result = intraday_outcome_attribution_summary([
            {**base, "signal_event_id": "one", "raw_return": 0.01},
            {**base, "signal_event_id": "two", "raw_return": -0.005},
        ])
        stage = next(item for item in result["items"] if item["dimension"] == "stage")
        self.assertEqual(stage["matured"], 2)
        self.assertEqual(stage["hit_rate"], 0.5)
        self.assertEqual(stage["evaluation_status"], "descriptive_only")
        self.assertEqual(result["validation_gate"]["matured_unique_signals"], 2)
        self.assertEqual(result["validation_gate"]["status"], "accumulating")

    def test_post_close_15_session_structures_are_explicitly_provisional(self):
        bars = []
        for index in range(15):
            close = 10.0 + (index % 3) * 0.05
            bars.append({"high": close + 0.08, "low": close - 0.08, "close": close,
                         "volume": 100 if index < 12 else 60, "adj_factor": 1.0})
        forming = post_close_forming_structure(bars)
        self.assertIn(forming["status"], {"forming", "not_ready"})
        self.assertEqual(forming["bar_count"], 15)
        self.assertIn("15日", forming["notice"])

        fresh = [dict(item) for item in bars]
        fresh[-6]["close"] = 10.0
        fresh[-4]["close"] = 10.15
        fresh[-2]["close"] = 10.25
        fresh[-1].update({"close": 10.62, "high": 10.70, "low": 10.50, "volume": 220})
        started = post_close_fresh_start_structure(fresh)
        self.assertEqual(started["status"], "started")
        self.assertGreaterEqual(started["metrics"]["volume_multiple_5d"], 1.5)

    def test_post_close_structures_refuse_mixed_adjustment_basis(self):
        bars = [{"high": 10.2, "low": 9.8, "close": 10.0, "volume": 100, "adj_factor": 1.0}
                for _ in range(30)]
        bars[-1].pop("adj_factor")
        result = daily_base_structure(bars)
        self.assertEqual(result["status"], "data_quality_blocked")
        self.assertIn("adj_factor_missing", result["quality_flags"])

    def test_limit_ladder_and_ground_to_sky_replay_keep_causal_checkpoints(self):
        self.assertEqual(limit_board_count("首板"), 1)
        self.assertEqual(limit_board_count("8天6板"), 6)
        daily = post_close_limit_daily_features([
            {"trading_date": date(2026, 8, 10), "open": 11.57, "high": 11.57, "low": 11.57,
             "close": 11.57, "pre_close": 12.85, "volume": 26000, "selected_provider": "super_get"},
            {"trading_date": date(2026, 8, 11), "open": 12.0, "high": 12.73, "low": 10.41,
             "close": 12.73, "pre_close": 11.57, "volume": 1340000, "selected_provider": "super_get"},
        ])
        self.assertTrue(daily["ground_to_sky_daily_shape"])
        prices = [12.0, 11.2, 10.6, 10.41, 10.41, 10.45, 10.46, 10.50, 10.55, 10.57,
                  10.78, 10.89, 11.25, 11.10, 11.49, 11.96, 12.19, 11.70, 12.14, 12.73]
        times = [f"09{30 + index:02d}" for index in range(10)] + [f"13{index:02d}" for index in range(10)]
        volumes = [1000] * 10 + [4500, 5000, 4200, 1800, 2200, 4000, 3500, 2000, 2600, 6000]
        cumulative_volume = 0
        cumulative_amount = 0.0
        rows = []
        for minute, price, volume in zip(times, prices, volumes, strict=True):
            cumulative_volume += volume
            cumulative_amount += price * volume * 100
            rows.append({"time": minute, "close": price, "volume_lot": volume,
                         "amount": price * volume * 100, "cumulative_volume_lot": cumulative_volume,
                         "cumulative_amount": cumulative_amount,
                         "vwap": cumulative_amount / (cumulative_volume * 100), "is_complete": True})
        pattern = intraday_limit_lift_pattern(rows, daily)
        self.assertEqual(pattern["status"], "completed")
        self.assertIn("ground_to_sky_reversal", pattern["pattern_tags"])
        self.assertIsNotNone(pattern["deep_reversal_impulse"])
        self.assertIsNotNone(pattern["previous_close_reclaim"])
        self.assertIsNotNone(pattern["previous_close_acceptance"])
        self.assertEqual(pattern["limit_reclaim"]["time"], "1309")

    def test_deep_reversal_research_alerts_impulse_then_previous_close_acceptance(self):
        watch = {"symbol": "000779.SZ", "entry_price": None, "available_quantity": 0,
                 "alert_on_entry": True, "alert_on_exit": False,
                 "metadata": {"reversal_research": {"enabled": True, "label": "ground_to_sky"}}}
        impulse_quote = {"price": 9.4, "pct_change": -6.0, "volume_ratio": 3, "turnover_rate": 12,
                         "main_net_inflow": 10, "main_flow_percentile": 0.95}
        impulse_minute = {"return_1m_pct": 0.8, "return_3m_pct": 2.0, "minute_volume_multiple": 3.5,
                          "above_vwap_pct": 0.5, "recovery_from_session_low_pct": 4.4,
                          "session_low_price": 9.0}
        impulse = intraday_signal_rules(watch, impulse_quote, {"price": 9.2}, None, impulse_minute, {})
        self.assertEqual(impulse[0]["signal_key"], "000779.SZ:watch:deep_reversal_impulse_v1")
        reclaim_quote = {"price": 10.1, "pct_change": 1.0, "volume_ratio": 3, "turnover_rate": 18,
                         "main_net_inflow": 20, "main_flow_percentile": 0.96}
        reclaim_minute = {"return_1m_pct": 0.6, "return_3m_pct": 1.0, "minute_volume_multiple": 2.0,
                          "above_vwap_pct": 3.0, "recovery_from_session_low_pct": 12.2,
                          "session_low_price": 9.0}
        reclaim = intraday_signal_rules(watch, reclaim_quote, {"price": 9.9}, None, reclaim_minute, {})
        self.assertEqual(reclaim[0]["signal_key"], "000779.SZ:watch:deep_reversal_previous_close_acceptance_v1")
        self.assertTrue(reclaim[0]["stage_upgrade"])

    def test_opening_ladder_drive_uses_price_checkpoints_not_sealed_board_volume(self):
        daily = {"pre_close": 9.53, "close": 10.48, "close_pct": 9.97}
        prices = [9.56, 9.65, 9.78, 9.95, 10.02, 10.10, 10.32, 10.38, 10.43, 10.48,
                  10.48, 10.48, 10.48, 10.48, 10.48, 10.48]
        volumes = [8000, 9000, 10000, 11000, 13000, 12000, 15000, 18000, 22000, 40000,
                   100, 100, 100, 100, 50000, 100]
        rows = [{"time": f"09{30 + index:02d}", "close": price, "volume_lot": volume}
                for index, (price, volume) in enumerate(zip(prices, volumes, strict=True))]
        pattern = intraday_limit_lift_pattern(rows, daily)
        self.assertIn("opening_ladder_drive", pattern["pattern_tags"])
        self.assertEqual(pattern["opening_drive"]["first_four_pct_time"], "0933")
        self.assertEqual(pattern["opening_drive"]["first_eight_pct_time"], "0936")
        self.assertEqual(pattern["limit_reclaim"]["time"], "0939")
        self.assertGreaterEqual(pattern["post_limit_volume_spike_minutes"], 1)

    def test_daily_deep_low_without_minute_close_extreme_stays_unconfirmed(self):
        daily = {"pre_close": 14.48, "close": 15.93, "close_pct": 10.01, "low_pct": -9.53}
        prices = [14.0, 13.77, 13.57, 13.42, 13.71, 13.58, 13.82, 14.09, 14.18, 14.0,
                  14.16, 14.45, 14.9, 15.11, 15.42, 15.93]
        rows = [{"time": f"09{30 + index:02d}", "close": price, "volume_lot": 1000 + index * 100}
                for index, price in enumerate(prices)]
        pattern = intraday_limit_lift_pattern(rows, daily)
        self.assertIn("intraminute_extreme_not_in_minute_close", pattern["pattern_tags"])
        self.assertEqual(pattern["deep_discount_stabilization"]["time"], "0936")
        self.assertEqual(pattern["deep_discount_stabilization"]["confirmation"], "price_only_unconfirmed")
        self.assertIsNone(pattern["deep_reversal_impulse"])

    def test_eac_first_observation_alerts_before_second_scan(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        signal = {"signal_type": "watch", "hard": False, "alert_on_first_observation": True}
        self.assertEqual(intraday_signal_event_state(
            signal, observed_at=now, latest_event_at=None, last_key_alerted_at=None, last_symbol_watch_alerted_at=None,
        ), "confirmed")

    def test_post_close_base_contraction_requires_a_ready_but_unbroken_range(self):
        closes = [9.7, 10.25, 9.8, 10.2, 9.75, 10.3, 9.85, 10.2, 9.9, 10.25,
                  9.8, 10.3, 9.9, 10.2, 10.0, 10.05, 10.10, 10.08, 10.12, 10.10,
                  10.14, 10.12, 10.16, 10.13, 10.15, 10.14, 10.18, 10.16, 10.20, 10.28]
        bars = []
        for index, close in enumerate(closes):
            narrow = index >= 15
            bars.append({"close": close, "high": 10.4 if narrow else close + 0.2,
                         "low": 9.6 if narrow and index % 5 == 0 else close - (0.08 if narrow else 0.2),
                         "volume": 50 if index >= 25 else 70 if narrow else 120, "adj_factor": 1.0})
        structure = daily_base_structure(bars)
        self.assertEqual(structure["status"], "ready")
        self.assertTrue(structure["components"]["volume_dry_up"])
        self.assertTrue(structure["components"]["near_resistance"])
        self.assertLess(structure["metrics"]["close_to_resistance_pct"], 3.0)

    def test_intraday_alert_cooldown_uses_last_alert_not_last_suppressed_event(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
        watch = {"signal_type": "watch", "hard": False}
        self.assertEqual(intraday_signal_event_state(
            watch, observed_at=now, latest_event_at=now - timedelta(seconds=30),
            last_key_alerted_at=now - timedelta(minutes=1), last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "suppressed")
        # A different watch key is also suppressed while a recent watch for the
        # same symbol is cooling down, but a true entry remains eligible.
        self.assertEqual(intraday_signal_event_state(
            watch, observed_at=now, latest_event_at=None, last_key_alerted_at=None,
            last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "suppressed")
        stage_upgrade = {"signal_type": "watch", "hard": False, "independent_confirmation": True,
                         "stage_upgrade": True}
        self.assertEqual(intraday_signal_event_state(
            stage_upgrade, observed_at=now, latest_event_at=None, last_key_alerted_at=None,
            last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "confirmed")
        entry = {"signal_type": "entry", "hard": False, "independent_confirmation": True}
        self.assertEqual(intraday_signal_event_state(
            entry, observed_at=now, latest_event_at=None, last_key_alerted_at=None,
            last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "confirmed")

    def test_intraday_same_episode_realerts_only_after_material_change(self):
        now = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
        signal = {"signal_type": "watch", "hard": False, "score": 70,
                  "conditions": {"price": 10.0, "volume_ratio": 2.0, "main_net_inflow": 100}}
        prior = {"score": 65, "conditions": {"price": 10.0, "volume_ratio": 2.0, "main_net_inflow": 100}}
        self.assertEqual(intraday_signal_event_state(
            signal, observed_at=now, latest_event_at=now - timedelta(seconds=30),
            last_key_alerted_at=now - timedelta(minutes=20), last_symbol_watch_alerted_at=None,
            last_key_alert=prior,
        ), "suppressed")
        signal["conditions"] = {**signal["conditions"], "price": 10.2}
        self.assertEqual(intraday_signal_event_state(
            signal, observed_at=now, latest_event_at=now - timedelta(seconds=30),
            last_key_alerted_at=now - timedelta(minutes=20), last_symbol_watch_alerted_at=None,
            last_key_alert=prior,
        ), "confirmed")
        # A condition that disappeared beyond the confirmation window is a
        # new episode even if the prior alert was in the same session.
        self.assertEqual(intraday_signal_event_state(
            {"signal_type": "entry", "hard": False, "score": 70, "conditions": {}},
            observed_at=now, latest_event_at=now - timedelta(minutes=6),
            last_key_alerted_at=now - timedelta(minutes=20), last_symbol_watch_alerted_at=None,
            last_key_alert=prior,
        ), "confirming")

    def test_live_policy_gate_blocks_new_entry_during_broad_risk_off(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0}, {"price": 10},
            {"trade_constraints": {}}, {"market_state": "broad_risk_off", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertIn("broad_risk_off_blocks_new_entry", result["reason_codes"])

    def test_live_policy_gate_keeps_unsellable_hard_stop_as_risk_alert(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "exit"}, {"entry_price": 10, "available_quantity": 0}, {"price": 9},
            {"trade_constraints": {"limit_down": 8}}, {"market_state": "mixed_or_neutral"}, {"status": "confirmed"},
        )
        self.assertEqual(result["decision"], "risk_alert_only")
        self.assertTrue(result["allow_confirmation"])

    def test_intraday_outcome_metrics_keep_direction_and_adverse_path(self):
        prices = [Decimal("10.10"), Decimal("9.80"), Decimal("10.40")]
        long_metrics = intraday_signal_outcome_metrics(Decimal("10.00"), 1, prices)
        short_metrics = intraday_signal_outcome_metrics(Decimal("10.00"), -1, prices)
        self.assertEqual(intraday_signal_direction("entry"), 1)
        self.assertEqual(intraday_signal_direction("watch"), 1)
        self.assertEqual(intraday_signal_direction("reduce"), -1)
        self.assertEqual(intraday_signal_direction("exit"), -1)
        self.assertIsNone(intraday_signal_direction("data_issue"))
        self.assertEqual(long_metrics, {
            "raw_return": Decimal("0.04"),
            "maximum_favorable_excursion": Decimal("0.04"),
            "maximum_adverse_excursion": Decimal("-0.02"),
        })
        self.assertEqual(short_metrics, {
            "raw_return": Decimal("-0.04"),
            "maximum_favorable_excursion": Decimal("0.02"),
            "maximum_adverse_excursion": Decimal("-0.04"),
        })

    def test_sector_surge_replay_catches_xianglu_before_late_extension(self):
        rows = []
        prices = [38.60] * 20 + [38.80, 39.20, 39.43, 39.85]
        volumes = [1100] * 20 + [1494, 6325, 2482, 9286]
        cumulative_volume = 0
        cumulative_amount = 0.0
        for index, (price, volume) in enumerate(zip(prices, volumes, strict=True)):
            cumulative_volume += volume
            cumulative_amount += price * volume * 100
            rows.append({"time": f"{1341 + index:04d}", "close": price, "volume_lot": volume,
                         "amount": price * volume * 100, "cumulative_volume_lot": cumulative_volume,
                         "cumulative_amount": cumulative_amount, "vwap": cumulative_amount / (cumulative_volume * 100),
                         "is_complete": True})
        feature = intraday_minute_features(rows)
        self.assertGreater(feature["minute_volume_multiple"], 8)
        peers = {
            "002378.SZ": {"return_1m_pct": 0.782, "return_3m_pct": 1.425, "minute_volume_multiple": 7.531},
            "000657.SZ": {"return_1m_pct": 0.861, "return_3m_pct": 1.320, "minute_volume_multiple": 2.365},
            "600549.SH": {"return_1m_pct": 0.702, "return_3m_pct": 1.235, "minute_volume_multiple": 1.955},
        }
        context = intraday_peer_context(list(peers), peers)
        watch = {"symbol": "002842.SZ", "entry_price": None, "available_quantity": 0, "alert_on_entry": True,
                 "alert_on_exit": False, "metadata": {"surge_strategy": {"enabled": True, "sector_label": "同花顺钨"}}}
        leader_feature = {"return_1m_pct": 1.031, "return_3m_pct": 0.979, "minute_volume_multiple": 5.597,
                          "above_vwap_pct": 1.94}
        empty_context = intraday_peer_context(list(peers), {symbol: {**item, "return_1m_pct": 0}
                                                             for symbol, item in peers.items()})
        leader_quote = {"price": 39.20, "pct_change": 2.14, "volume_ratio": 1.2, "turnover_rate": 15,
                        "main_net_inflow": 10, "main_flow_percentile": 0.7}
        leader = intraday_signal_rules(watch, leader_quote, {"price": 38.80}, None, leader_feature, empty_context)[0]
        self.assertEqual(leader["signal_key"], "002842.SZ:watch:leader_burst_v1")
        quote = {"price": 39.85, "pct_change": 3.83, "volume_ratio": 1.4, "turnover_rate": 18,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        signal = intraday_signal_rules(watch, quote, {"price": 39.43}, None, feature, context)[0]
        self.assertEqual(signal["signal_key"], "002842.SZ:entry:sector_surge_v1")
        self.assertTrue(signal["independent_confirmation"])
        quote["pct_change"] = 6.6
        self.assertFalse(any(item["signal_key"] == "002842.SZ:entry:sector_surge_v1"
                             for item in intraday_signal_rules(watch, quote, {"price": 39.85}, None, feature, context)))

    def test_provider_decoder_accepts_object_rows(self):
        rows = _decode_rows({"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [
            {"ts_code": "600000.SH", "trade_date": "20220429"},
        ]}})
        self.assertEqual(rows, [{"ts_code": "600000.SH", "trade_date": "20220429"}])
        realtime_rows = _decode_rows({"code": 0, "data": [{"ts_code": "000636.SZ", "time": "2026-08-10 11:26:00"}]})
        self.assertEqual(realtime_rows[0]["time"], "2026-08-10 11:26:00")

    def test_intraday_strategy_scoring_uses_relative_ranks_and_marks_extension(self):
        self.assertEqual(strategy_rank([None, None]), {})
        self.assertEqual(strategy_rank([-1.0])[0], 0.0)
        items = [
            {"taxonomy_key": "eastmoney_concept", "sector_key": "A", "label": "资金流入", "net_inflow": 50,
             "change_pct": 2.0, "mapped_members": 2, "top_stocks": [{"symbol": "000001.SZ", "name": "甲", "main_net_inflow": 30,
             "volume_ratio": 3.5, "turnover_rate": 8, "pct_change": 4, "turnover": 100}]},
            {"taxonomy_key": "eastmoney_concept", "sector_key": "B", "label": "资金流出", "net_inflow": -10,
             "change_pct": -1.0, "mapped_members": 2, "top_stocks": [{"symbol": "000002.SZ", "name": "乙", "main_net_inflow": -5,
             "volume_ratio": 5, "turnover_rate": 30, "pct_change": 9, "turnover": 80}]},
        ]
        regime, metrics = strategy_market_regime(items)
        self.assertEqual(regime, "neutral")
        self.assertEqual(metrics["known_board_flows"], 2)
        candidates = strategy_intraday_candidates(items, 10)
        self.assertEqual(candidates[0]["symbol"], "000001.SZ")
        self.assertEqual(candidates[0]["decision"], "research_candidate")
        declining = next(item for item in candidates if item["symbol"] == "000002.SZ")
        self.assertEqual(declining["decision"], "no_trade")
        self.assertIn("price_extension", declining["risk_flags"])

    def test_market_state_distinguishes_defensive_rotation_from_broad_direction(self):
        items = [
            {"label": "贵金属", "net_inflow": 10, "change_pct": 2.0},
            {"label": "银行", "net_inflow": 8, "change_pct": 0.5},
            {"label": "半导体", "net_inflow": -12, "change_pct": 0.2},
            {"label": "通信设备", "net_inflow": -9, "change_pct": -0.5},
        ]
        state, metrics = strategy_market_state(items)
        self.assertEqual(state, "rotation_defensive")
        self.assertEqual(metrics["defensive_inflow_boards"], ["贵金属", "银行"])
        self.assertEqual(metrics["technology_outflow_boards"], ["半导体", "通信设备"])

    def test_tencent_snapshot_quotes_keep_inferred_exchange_date(self):
        quotes = tencent_snapshot_quotes([
            {"code": "sh600176", "name": "中国巨石", "zxj": "43.28", "zdf": "-2.48", "lb": "1.2", "hsl": "3.0", "zljlr": "12", "turnover": "100"},
        ], date(2026, 8, 10))
        self.assertEqual(quotes[0]["ts_code"], "600176.SH")
        self.assertEqual(quotes[0]["trade_date"], "20260810")
        self.assertEqual(quotes[0]["volume_ratio"], 1.2)
        self.assertEqual(quotes[0]["turnover_rate"], 3.0)
        self.assertEqual(quotes[0]["main_net_inflow"], 12.0)
        self.assertTrue(quotes[0]["source_session_date_inferred"])

    def test_free_provider_symbol_routing_is_explicit(self):
        self.assertEqual(eastmoney_secid("603580.SH"), "1.603580")
        self.assertEqual(eastmoney_secid("000636.SZ"), "0.000636")
        self.assertEqual(tencent_symbol("603580.SH"), "sh603580")
        self.assertEqual(tencent_symbol("000636.SZ"), "sz000636")
        self.assertTrue({"eastmoney_free", "tencent_free", "sina_free", "cninfo_free", "akshare", "xinhua_finance"}.issubset(
            {item["provider_key"] for item in free_provider_status()}
        ))
        self.assertEqual(cninfo_stock_param("000636.SZ")["stock"], "000636,gssz0000636")
        self.assertEqual(cninfo_stock_param("600519.SH")["stock"], "600519,gssh0600519")
        self.assertEqual(classify_announcement_title("关于回购公司股份的公告"), "shareholder_event")
        self.assertEqual(classify_announcement_title("重大事项公告"), "corporate_action")

    def test_sina_batch_parser_and_market_snapshot_quality_are_explicit(self):
        rows = parse_sina_quote_batch(
            'var hq_str_sz000636="风华高科,20,19.5,20.2,20.5,19.7,0,0,123,456,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-08-10,11:30:00";',
            {"sz000636": "000636.SZ"},
        )
        self.assertEqual(rows[0]["ts_code"], "000636.SZ")
        summary = summarize_quotes(rows)
        self.assertEqual(summary["advancers"], 1)
        self.assertAlmostEqual(summary["median_change_pct"], 3.5897, places=4)
        status, eligible, flags = snapshot_status(universe_count=1200, quote_count=1180, minimum_universe=1000,
                                                   minimum_coverage=0.95, licensed_providers=set(), observed_providers={"sina_free"})
        self.assertEqual(status, "degraded")
        self.assertFalse(eligible)
        self.assertIn("no_licensed_realtime_market_feed", flags)

    def test_public_market_batch_is_opt_in_and_bounded(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(market_snapshot_public_quote_settings(), {"enabled": False, "batch_size": 80, "concurrency": 2})
        with patch.dict("os.environ", {"MARKET_SNAPSHOT_ENABLE_PUBLIC_BATCH": "true", "MARKET_SNAPSHOT_PUBLIC_BATCH_SIZE": "999", "MARKET_SNAPSHOT_PUBLIC_CONCURRENCY": "0"}, clear=True):
            self.assertEqual(market_snapshot_public_quote_settings(), {"enabled": True, "batch_size": 200, "concurrency": 1})

    def test_sector_catalog_sync_is_explicitly_bounded(self):
        self.assertEqual(ths_taxonomy_key("N"), "ths_index_n")
        self.assertEqual(SectorCatalogSyncRequest(index_type="I").member_limit, 0)
        self.assertEqual(ConceptMemberSyncRequest().member_limit, 25)
        self.assertEqual(ConceptMemberSyncRequest(member_offset=50).member_offset, 50)
        self.assertEqual(EastmoneyBoardMemberSyncRequest(kind="industry").member_limit, 25)
        self.assertEqual(IntradaySectorReportRequest().hydrate_top_boards, 0)
        self.assertEqual(IntradaySectorReportRequest(hydrate_top_boards=3).hydrate_top_boards, 3)
        self.assertEqual(eastmoney_member_symbol({"代码": "600519"}), "600519.SH")
        self.assertEqual(eastmoney_member_symbol({"代码": "300750"}), "300750.SZ")
        self.assertEqual(eastmoney_member_symbol({"代码": "920870"}), "920870.BJ")
        self.assertIsNone(eastmoney_member_symbol({"代码": "invalid"}))
        with self.assertRaises(ValidationError):
            SectorCatalogSyncRequest(sync_members=True)
        with self.assertRaises(ValidationError):
            SectorCatalogSyncRequest(member_limit=1)
        with self.assertRaises(ValidationError):
            SectorCatalogSyncRequest(all_types=True, sync_members=True, member_limit=1)

    def test_offline_minute_row_uses_shanghai_time_and_validates_ohlc(self):
        parsed = offline_minute_row({
            "ts_code": "600519.SH", "datetime": "2026-08-04 09:31:00",
            "open": "1400", "high": "1402", "low": "1399", "close": "1401", "vol": "10",
        })
        self.assertEqual(parsed["symbol"], "600519.SH")
        self.assertEqual(parsed["bar_time"].isoformat(), "2026-08-04T01:31:00+00:00")
        self.assertEqual(str(parsed["volume"]), "10")
        with self.assertRaises(ValueError):
            offline_minute_row({"symbol": "600519.SH", "datetime": "2026-08-04 09:31:00", "open": "10", "high": "9", "low": "8", "close": "10"})
        with self.assertRaises(ValidationError):
            OfflineMinuteImportRequest(file_name="../outside.csv")

    def test_universe_and_provider_response_guards(self):
        universe = UniverseUpdateRequest(symbols=["603580.sh", "000636.SZ", "603580.SH"])
        self.assertEqual(universe.symbols, ["000636.SZ", "603580.SH"])
        with self.assertRaises(ValidationError):
            UniverseUpdateRequest(symbols=["艾艾精工"])
        self.assertTrue(looks_like_response_header([{"ts_code": "ts_code", "trade_date": "trade_date"}]))
        self.assertTrue(looks_like_response_header([
            {"ts_code": "ts_code", "trade_date": "trade_date"},
            {"ts_code": "ts_code", "trade_date": "trade_date"},
        ]))
        self.assertFalse(looks_like_response_header([{"ts_code": "603580.SH", "trade_date": "20260807"}]))
        self.assertFalse(realtime_rows_are_current("rt_k", [{"ts_code": "603580.SH", "trade_time": "20260807"}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_k", [{"ts_code": "000636.SZ", "close": 58.26}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_etf_k", [{"ts_code": "510300.SH", "close": 4.93}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_min", [{"ts_code": "000636.SZ", "updated_at": "2026-08-10T10:16:36.000"}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_min_daily", [{"ts_code": "000636.SZ", "time": "2026-08-10 15:00:00"}], date(2026, 8, 10)))
        china = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        observed_at = __import__("datetime").datetime(2026, 8, 11, 11, 20, tzinfo=china)
        self.assertTrue(realtime_rows_are_current(
            "rt_min", [{"ts_code": "600176.SH", "time": "2026-08-11 11:19:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertFalse(realtime_rows_are_current(
            "rt_min", [{"ts_code": "600176.SH", "time": "2026-08-11 15:00:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertFalse(realtime_rows_are_current(
            "rt_min_daily", [{"ts_code": "600176.SH", "time": "2026-08-11 15:00:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertFalse(realtime_rows_are_current(
            "rt_idx_min", [{"ts_code": "000001.SH", "time": "2026-08-11 15:00:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertTrue(realtime_rows_are_current("daily", [{"ts_code": "000636.SZ", "trade_date": "20260807"}], date(2026, 8, 10)))

    def test_technical_summary_requires_prices_and_exposes_trend_inputs(self):
        empty = technical_summary([])
        self.assertEqual(empty["status"], "insufficient_market_data")
        rows = [{"trade_date": f"202608{day:02d}", "close": 10 + day / 10} for day in range(1, 22)]
        summary = technical_summary(rows)
        self.assertEqual(summary["status"], "ready")
        self.assertIsNotNone(summary["sma_20"])


if __name__ == "__main__":
    unittest.main()

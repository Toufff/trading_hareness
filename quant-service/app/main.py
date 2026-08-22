from __future__ import annotations
import asyncio
from bisect import bisect_right
import csv
import functools
import hashlib
import json
import math
import os
import re
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from time import monotonic
from typing import Any, Awaitable, Callable, Literal, Mapping
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, model_validator
from psycopg.types.json import Json

from .akshare_provider import (
    AkShareProviderError,
    akshare_analyst_heat_supplements,
    akshare_block_trade_supplements,
    akshare_board_supplements,
    akshare_corporate_risk_supplements,
    akshare_daily,
    akshare_eastmoney_board_catalog,
    akshare_eastmoney_board_flow,
    akshare_eastmoney_board_members,
    akshare_index_fund_supplements,
    akshare_lhb_events,
    akshare_lhb_supplements,
    akshare_live_limit_up_pool_events,
    akshare_limit_pool_events,
    akshare_macro_cross_asset_supplements,
    akshare_market_summary,
    akshare_market_breadth,
    akshare_moneyflow_supplements,
    akshare_status,
    akshare_strong_pool_events,
    akshare_tencent_all_a_spot,
)
from .analysis import EXTRACTOR_VERSION, as_utc, extract_signals, keywords, normalize_symbol
from .capability_registry import api_capability
from .database import AsyncDatabase, Database
from .daily_bar_repository import exchange_for, provider_priority, upsert_daily_bar
from .public_market_repository import (
    persist_free_quote as _persist_free_quote,
    persist_free_quotes as _persist_free_quotes,
    persist_market_events as _persist_market_events,
    persist_public_observations as _persist_public_observations,
    recent_market_events as _recent_market_events,
)
from .factor_sql_lab import evaluate_factor_set, run_multi_factor_strategy_sql
from .analyst_promotion import analyst_live_promotion
from .research_prices import adjusted_bars
from .live_policy import live_policy_gate
from .numeric_utils import decimal_or_none, intraday_number
from .intraday_clock import eac_window as pure_intraday_eac_window
from .intraday_clock import feature_clock as pure_intraday_feature_clock
from .intraday_clock import minute_bucket as pure_intraday_minute_bucket
from .intraday_features import minute_features as pure_intraday_minute_features
from .intraday_features import annotate_flow_snapshot_provenance as pure_annotate_flow_snapshot_provenance
from .intraday_features import mapped_watchlist_peers as pure_mapped_watchlist_peers
from .intraday_features import peer_context as pure_intraday_peer_context
from .intraday_features import strategy_session_rows as pure_strategy_session_rows
from .intraday_decision_card_read_model import decision_card as read_intraday_decision_card
from .intraday_volume_profiles import attach_volume_time_profile as pure_attach_volume_time_profile
from .intraday_volume_profiles import volume_time_profile as pure_intraday_volume_time_profile
from .intraday_volume_profiles import volume_time_profiles as pure_intraday_volume_time_profiles
from .intraday_minute_provider_service import fetch_bounded_minute_context
from .intraday_cross_section import SharedAsyncSnapshot
from .intraday_state_machine import classify_setup_state as classify_intraday_setup_state
from .intraday_factor_contracts import (
    INTRADAY_FACTOR_CONTRACT_VERSION,
    contracts_for_signal as intraday_factor_contracts_for_signal,
)
from .intraday_signal_contracts import signal_contract as intraday_signal_contract
from .post_close_limit_features import limit_daily_features as pure_limit_daily_features
from .post_close_limit_features import board_count as pure_limit_board_count
from .watchlist_daily_factors import watchlist_daily_factors as pure_watchlist_daily_factors
from .watchlist_daily_factors import watchlist_daily_factors_by_symbol as pure_watchlist_daily_factors_by_symbol
from .watchlist_main_wave_v2 import (
    STRATEGY_KEY as WATCHLIST_MAIN_WAVE_STRATEGY_KEY,
    latest_shadow_priors_v2,
    main_wave_v2_shadow_signal,
    run_watchlist_main_wave_v2_research,
)
from .watchlist_countertrend_rebound import (
    STRATEGY_KEY as WATCHLIST_REBOUND_STRATEGY_KEY,
    countertrend_rebound_failure_reduce_signal,
    countertrend_rebound_realtime_signal,
    latest_rebound_priors,
    run_countertrend_rebound_research,
)
from .intraday_decision_context import (
    decision_context as intraday_decision_context,
    invalidate_intraday_probability_profiles,
    load_intraday_probability_profiles,
    probability_for_signal as intraday_probability_for_signal,
)
from .feature_snapshot_repository import materialize_feature_snapshot
from .intraday_limit_lift import intraday_limit_lift_pattern as pure_intraday_limit_lift_pattern
from .intraday_attribution import signal_attribution as pure_signal_attribution
from .intraday_breakout import eac_acceptance_assessment as pure_eac_acceptance_assessment
from .intraday_breakout import upside_research_assessment as pure_upside_research_assessment
from .intraday_signal_rules import signal_rules as pure_intraday_signal_rules
from .intraday_outcome_attribution import outcome_attribution_summary as pure_outcome_attribution_summary
from .post_close_pattern_score import review_score as pure_pattern_review_score
from .post_close_pattern_candidates import select_candidates as pure_post_close_pattern_candidates
from .post_close_candidate_screen import screen_candidates as pure_post_close_screen_candidates
from .post_close_evidence import exact_board_context as pure_exact_board_context, lhb_context as pure_lhb_context
from .post_close_strategy_service import (
    candidates as persisted_post_close_strategy_candidates,
    completed_for_date as persisted_post_close_strategy_completed_for_date,
    retry_window as post_close_strategy_retry_window,
    run as persisted_run_post_close_strategy,
)
from .post_close_scheduler import PostCloseSchedulerDependencies, post_close_strategy_scheduler
from .strategy_review_scheduler import StrategyReviewSchedulerDependencies, strategy_review_scheduler
from .analyst_market_review import build_recorded_analyst_market_review
from .strategy_pattern_read_model import latest_strategy_pattern_mining as read_latest_strategy_pattern_mining
from .intraday_outcome_settlement import settle as persist_intraday_outcome_settlement
from .tushare_normalization import normalize_rows as pure_normalize_tushare_rows
from .market_regimes import (
    STRATEGY_INDEX_SYMBOLS,
    strategy_index_regime as pure_strategy_index_regime,
    strategy_market_regime as pure_strategy_market_regime,
    strategy_market_state as pure_strategy_market_state,
    strategy_rank as pure_strategy_rank,
)
from .free_market_providers import (
    FreeProviderError,
    cninfo_announcements,
    eastmoney_daily,
    eastmoney_quote,
    eastmoney_watch_flow_quotes,
    free_provider_status,
    sina_quote,
    sina_quotes,
    tencent_daily,
    tencent_intraday_minutes,
    tencent_order_book_quotes,
)
from .order_book_features import aggregate_order_book_observations, order_book_observation
from .market_snapshots import snapshot_status, summarize_quotes
from .market_flow_repository import (
    persist_intraday_market_flow_feature,
    persist_market_snapshot_flow_feature,
    rebuild_stored_market_flow_features,
)
from .intraday_alerts import daily_strategy_summary_text, delivery_health_recovery_text, intraday_alert_text
from .board_rotation import board_rotation_candidates, board_rotation_still_directional
from .board_stock_mining import board_stock_mining_candidates
from .board_stock_mining_repository import persist_board_stock_mining_run
from .limit_linkage_mining import limit_linkage_candidates
from .limit_linkage_mining_repository import persist_limit_linkage_mining_run
from .board_curve_read_model import board_display_slots as _board_display_slots
from .board_curve_read_model import intraday_board_flow_curves as read_intraday_board_flow_curves
from .board_curve_read_model import latest_close_sector_review_report as read_latest_close_sector_review_report
from . import research_catalog_read_model as research_catalog_reads
from . import sector_read_model as sector_reads
from . import intraday_evidence_read_model as intraday_evidence_reads
from . import market_result_read_model as market_result_reads
from .intraday_outcome_read_model import latest_intraday_outcomes as read_latest_intraday_outcomes
from .http_clients import (alert_http_client_status, close_http_clients, provider_http_client_status,
                           public_http_client_status, remote_archive_http_client_status, start_http_clients)
from .network_health import network_state
from .alert_transport import post_feishu_alert_text
from .intraday_schedule import (
    intraday_board_curve_clock_session,
    intraday_board_curve_enabled,
    intraday_board_curve_retention_days,
    intraday_board_rotation_retention_days,
    intraday_board_refresh_interval_seconds,
    intraday_effective_scan_interval_seconds,
    intraday_fast_quote_retention_days,
    intraday_high_frequency_window,
    intraday_next_monitor_delay_seconds,
    intraday_realtime_validation_slice,
    intraday_rule_input_retention_days,
    intraday_runtime_service_state,
    intraday_scan_interval_seconds,
    intraday_super_get_fast_interval_seconds,
    intraday_super_get_fast_max_in_flight,
    intraday_super_get_fast_max_symbols,
    intraday_watchlist_capacity,
)
from .intraday_monitor_service import run_intraday_monitor_loop
from .intraday_fast_quote_service import cross_source_confirmation, run_intraday_fast_quote_loop
from .study_realtime import _row_trade_date, _row_trade_datetime, looks_like_response_header, realtime_rows_are_current
from .provider_health import (
    provider_error_availability,
    record_provider_api_capability,
    record_provider_failure,
    record_provider_success,
)
from .technical_analysis import technical_summary
from .post_close_structures import (
    POST_CLOSE_STRATEGY_MODEL_VERSION,
    daily_base_structure,
    post_close_forming_structure,
    post_close_fresh_start_structure,
)
from .runtime_tasks import LoopRuntimeRegistry, observe_completed_task, supervise_leased_loop, supervise_loop
from .intraday_outcomes import (
    INTRADAY_OUTCOME_HORIZONS,
    intraday_outcome_cutoff,
    intraday_signal_direction,
    intraday_signal_outcome_metrics,
    a_share_return_decomposition,
)
from .intraday_scan_repository import (
    first_eac_breakout_events,
    persist_intraday_scan_terminal,
    previous_quote_frames,
)
from .intraday_rule_snapshot_repository import persist_rule_input_snapshot, prune_rule_input_evidence
from .intraday_event_retention import ephemeral_signal_retention_days, prune_ephemeral_signal_events
from .market_session_repository import (
    realtime_market_session as read_realtime_market_session,
    realtime_market_session_async as read_realtime_market_session_async,
)
from .intraday_signal_policy import (
    signal_event_state as intraday_signal_event_state,
    signal_material_change as intraday_signal_material_change,
)
from .contextual_policy_learning import contextual_bandit_policy_review
from .paper_execution import paper_decision_payload, persist_barrier_outcome, persist_paper_decision, triple_barrier_label
from .paper_portfolio import paper_risk_gate, persist_portfolio_snapshot
from .paper_execution_service import accept_paper_decision, configure_paper_account, roll_paper_positions_sellable
from .analyst_prompt_lab import (
    evaluate_prompt_variant,
    label_prompt_candidate,
    materialize_intraday_analyst_outcomes,
    materialize_prompt_candidates,
)
from .analyst_action_outcomes import materialize_anqiang_action_replay_outcomes
from .strategy_contracts import LabelSpec
from .strategy_ablation import ablation_scores
from .episode_lifecycle import clear_stale_signal_episodes, ensure_signal_episode
from .runtime_resources import (
    DEFAULT_HOT_DATABASE_SOFT_BYTES,
    DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
    bounded_storage_budget_bytes,
    bounded_storage_ratio,
    managed_directory_bytes,
    research_storage_governance,
    runtime_resource_status,
)
from .health_read_model import DatabaseUnavailableError, HealthDependencies, health_payload as read_health_payload
from .replay_readiness import historical_replay_readiness
from . import research_capacity
from .feature_read_repository import analyst_feature as read_analyst_feature
from .feature_read_repository import latest_tushare_row as read_latest_tushare_row
from .feature_read_repository import market_regime as read_market_regime
from .analyst_text_features import analyst_text_factor_summary as read_analyst_text_factor_summary
from .intraday_status_read_model import IntradayStatusDependencies, intraday_services_status_payload as read_intraday_services_status_payload, intraday_services_status_payload_async as read_intraday_services_status_payload_async
from .routers.provider_status import build_provider_status_router
from .routers.research_readiness import build_research_readiness_router
from .routers.intraday_status import build_intraday_status_router
from .routers.analyst_reads import build_analyst_reads_router
from .routers.analyst_trade_action_reads import build_analyst_trade_action_reads_router
from .routers.analyst_action_outcomes import build_analyst_action_outcomes_router
from .routers.analyst_skill_reads import build_analyst_skill_reads_router
from .routers.analyst_research_reads import build_analyst_research_reads_router
from .routers.automation_reads import build_automation_reads_router
from .security import remote_archive_sync_bearer_allowed, write_access_allowed
from .automation_run_repository import run_recorded
from .daily_strategy_summary_service import (
    build_daily_strategy_summary as build_daily_strategy_summary_projection,
    terminal_for_exchange_date as daily_summary_terminal_isolated,
)
from .daily_strategy_summary_scheduler import (
    DailyStrategySummarySchedulerDependencies,
    daily_strategy_summary_scheduler,
)
from .strategy_decision_service import run as run_strategy_decision_isolated
from .strategy_review_service import build as build_strategy_review_isolated, completed_for_checkpoint as review_checkpoint_completed_isolated
from .routers.event_reads import build_event_reads_router
from .routers.strategy_reads import build_strategy_reads_router
from .routers.paper_reads import build_paper_reads_router
from .routers.paper_actions import build_paper_actions_router
from .routers.analyst_prompt_lab import build_analyst_prompt_lab_router
from .routers.strategy_pattern_reads import build_strategy_pattern_reads_router
from .routers.board_rotation_reads import build_board_rotation_reads_router
from .routers.board_stock_mining_reads import build_board_stock_mining_reads_router
from .routers.limit_linkage_mining_reads import build_limit_linkage_mining_reads_router
from .routers.board_curve_reads import build_board_curve_reads_router
from .routers.research_catalog_reads import build_research_catalog_reads_router
from .routers.intraday_outcome_reads import build_intraday_outcome_reads_router
from .routers.sector_reads import build_sector_reads_router
from .routers.intraday_evidence_reads import build_intraday_evidence_reads_router
from .routers.market_result_reads import build_market_result_reads_router
from .routers.market_flow_reads import build_market_flow_reads_router
from .routers.provider_actions import ProviderActionDependencies, build_provider_actions_router
from .routers.market_actions import MarketActionDependencies, build_market_actions_router
from .routers.intraday_actions import IntradayActionDependencies, build_intraday_actions_router
from .routers.sector_actions import SectorActionDependencies, build_sector_actions_router
from .routers.strategy_actions import StrategyActionDependencies, build_strategy_actions_router
from .routers.research_actions import ResearchActionDependencies, build_research_actions_router
from .routers.ingestion_actions import IngestionActionDependencies, build_ingestion_actions_router
from .market_rules import a_share_limit_ratio, china_equity_session, china_futures_session, cn_today, is_st_security_name
from .request_models import (
    AkShareProbeRequest,
    AnalystResearchProfileRequest,
    AnalystSyncCursorUpdate, AnalystSyncGlobalCursorUpdate,
    AnnouncementSyncRequest,
    AllBoardMemberBackfillRequest,
    BarsImport,
    BoardResearchRunRequest,
    ClaimReviewRequest,
    ConceptCandidateSyncRequest,
    ConceptMemberBackfillRequest,
    ConceptMemberSyncRequest,
    DailyBar,
    EastmoneyBoardMemberSyncRequest,
    FactorEvaluationRequest,
    FetchRunReconcileRequest,
    FullMarketDailySyncRequest,
    GenerateRequest,
    IntradayEventReplayRequest,
    IntradayRuleInputReplayRequest,
    HistoricalCoverageEstimateRequest,
    IntradayScanRequest,
    IntradaySectorReportRequest,
    IntradayWatchlistRequest,
    MarketSnapshotRequest,
    MarketFlowFeatureRebuildRequest,
    MarketUniverseSyncRequest,
    MinuteSessionCaptureRequest,
    OfflineMinuteImportRequest,
    PostCloseStrategyRequest,
    PostCloseRefreshRequest,
    RealtimeProbeRequest,
    RemoteReportImport,
    RemoteReportReprocessRequest,
    RemoteAnalystMessageImport,
    RemoteArchiveSyncRequest,
    RemoteMessageReprocessRequest,
    SnapshotRequest,
    StockStudyRequest,
    SectorCatalogSyncRequest,
    SectorFlowSyncRequest,
    StrategyBacktestRequest,
    StrategyDecisionRequest,
    StrategyPatternMiningRequest,
    StrategyReviewRequest,
    WatchlistMainWaveResearchRequest,
    TushareFetchRequest,
    TushareCapabilityAuditRequest,
    TushareSyncRequest,
    UniverseUpdateRequest,
)
from .remote_archive import classify_remote_text, remote_report_list_state, reprocess_remote_reports
from .remote_archive_actions import RemoteArchiveActions
from .market_snapshot_actions import MarketSnapshotActions
from .intraday_sector_report_service import build_intraday_sector_report_from_membership as build_intraday_sector_report_from_membership_isolated
from .intraday_sector_report_orchestrator import run as run_intraday_sector_report_isolated
from .cninfo_announcement_actions import CninfoAnnouncementActions
from .board_flow_capture_actions import BoardFlowCaptureActions
from .board_rotation_repository import BoardRotationRepository
from .intraday_minute_capture_actions import IntradayMinuteCaptureActions
from .intraday_event_replay_runner import run_recorded_signal_lifecycle_replay
from .intraday_rule_input_replay_runner import run_recorded_rule_input_replay
from .post_close_refresh import record_stage_with_receipt, run_refresh as run_post_close_refresh_orchestrated
from .daily_pipeline import run_pipeline as run_daily_pipeline_orchestrated
from .board_research_service import run as run_board_research_isolated
from .akshare_probe_service import run as run_akshare_probe_isolated
from .provider_probe_service import (
    audit_tushare_capabilities as audit_tushare_capabilities_isolated,
    probe_realtime as probe_realtime_sources_isolated,
)
from .recommendation_generation import generate as generate_recommendations_isolated
from .tushare_daily_sync import sync as sync_tushare_isolated
from .baostock_daily_sync import fetch_rows as fetch_baostock_rows_isolated, sync as sync_baostock_isolated
from .market_universe_sync import sync as sync_market_universe_isolated
from .full_market_daily_sync import sync as sync_full_market_daily_isolated
from .sector_catalog_sync import sync_all as sync_all_sector_catalogs_isolated
from .ths_sector_catalog_sync import sync as sync_ths_sector_catalog_isolated
from .eastmoney_sector_members_sync import sync as sync_eastmoney_sector_members_isolated
from .eastmoney_live_hydration import hydrate as hydrate_eastmoney_live_isolated
from .ths_sector_flows import sync_industry as sync_ths_industry_isolated, sync_concept_signals as sync_ths_concept_signals_isolated
from .outcome_recomputation import recompute as recompute_outcomes_isolated
from .ths_concept_members_sync import sync as sync_ths_concept_members_isolated
from .analyst_scorecards import recompute as recompute_scorecards_isolated
from .claim_review_service import review_claim as review_claim_isolated
from .analyst_trade_action_read_model import anqiang_trade_action_replay
from .analyst_skill_models import analyst_skill_profiles, rebuild_all_analyst_skill_profiles
from .analyst_expert_research import analyst_research_status, rebuild_analyst_research
from .telemetry import (
    CONTENT_TYPE_LATEST,
    db_pool_connections,
    generate_latest,
    intraday_scan_duration_seconds,
    provider_circuit_open,
    provider_shared_rate_limit_rejections_total,
    provider_shared_rate_limit_wait_seconds,
)
from .runtime_executors import ExecutorSaturatedError, run_akshare_blocking, run_database_blocking, runtime_executor_status, shutdown_runtime_executors
from .provider_rate_limits import provider_request_spacing_seconds, reserve_provider_rate_limit_slot
from .runtime_leases import (
    POST_CLOSE_REFRESH_LEASE_KEY,
    acquire_runtime_lease,
    background_loop_lease_seconds,
    post_close_refresh_lease_seconds,
    release_runtime_lease,
    renew_runtime_lease,
)
from .tushare_catalog import CORE_NORMALIZED_APIS, TUSHARE_CATALOG, catalog_counts, catalog_items
from .tushare_official import (
    AUDIT_FOCUS_APIS,
    HISTORICAL_MINUTE_APIS,
    REALTIME_MARKET_HOURS_APIS,
    default_probe_params,
    official_spec,
    realtime_probe_matrix,
)
from .tushare_providers import (
    SUPER_GET_VERIFIED_APIS,
    ProviderCallError,
    ProviderPreference,
    call_with_fallback,
    provider_candidates,
    provider_configs,
    provider_request_reservation_status,
    provider_status,
    safe_error_detail,
    configure_provider_request_reserver,
    super_get_executor_status,
    shutdown_super_get_executor,
)
from .universe_history import sync_universe_membership_history


db = Database()
async_db = AsyncDatabase(db)
_remote_archive_actions = RemoteArchiveActions(
    database=db,
    run_database_blocking=run_database_blocking,
    message_cursor_update=AnalystSyncGlobalCursorUpdate,
    report_cursor_update=AnalystSyncCursorUpdate,
)
_market_snapshot_actions = MarketSnapshotActions(db)
_cninfo_announcement_actions = CninfoAnnouncementActions(db)
_board_flow_capture_actions = BoardFlowCaptureActions(db)
_board_rotation_repository = BoardRotationRepository(db)
_intraday_minute_capture_actions = IntradayMinuteCaptureActions(db)
_research_storage_admission_cache: tuple[float, dict[str, Any]] | None = None


def local_research_storage_governance(database: Database = db) -> dict[str, Any]:
    """Measure managed research storage without mutating or pruning evidence."""
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint AS bytes
                 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='quant' AND c.relkind IN ('r','m','p')""",
        ).fetchone()
    data_dir = Path(os.getenv("QUANT_DATA_DIR", "/var/lib/quant"))
    warning_ratio = bounded_storage_ratio(os.getenv("QUANT_RESEARCH_STORAGE_WARNING_RATIO"), 0.80)
    return research_storage_governance(
        hot_database_bytes=int((row or {}).get("bytes") or 0),
        artifact_bytes=managed_directory_bytes(data_dir),
        research_budget_bytes=bounded_storage_budget_bytes(
            os.getenv("QUANT_RESEARCH_STORAGE_SOFT_BYTES"), DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
            DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
        ),
        hot_database_budget_bytes=bounded_storage_budget_bytes(
            os.getenv("QUANT_HOT_DATABASE_SOFT_BYTES"), DEFAULT_HOT_DATABASE_SOFT_BYTES,
            DEFAULT_HOT_DATABASE_SOFT_BYTES,
        ),
        warning_ratio=warning_ratio,
        stop_ratio=max(
            bounded_storage_ratio(os.getenv("QUANT_RESEARCH_STORAGE_STOP_RATIO"), 0.90), warning_ratio,
        ),
    )


async def nonessential_high_frequency_capture_allowed() -> tuple[bool, dict[str, Any]]:
    """Use a cached, local-only budget decision to protect finite research storage.

    This deliberately gates only optional raw evidence (depth, one-second
    cross-checks and board curves).  Watchlist price evaluation, risk alerts,
    outcomes and durable delivery keep running even at the stop watermark.
    """
    global _research_storage_admission_cache
    now = asyncio.get_running_loop().time()
    cached = _research_storage_admission_cache
    if cached is None or now - cached[0] >= 60.0:
        status = await run_database_blocking(local_research_storage_governance, timeout_seconds=10)
        _research_storage_admission_cache = (now, status)
    else:
        status = cached[1]
    return bool(status.get("allow_nonessential_high_frequency", True)), status


# The one-click post-close refresh has several write-heavy, ordered phases.
# A durable PostgreSQL lease serializes browser clicks and separate service
# instances without relying on one process's asyncio state.
def legacy_schema_bootstrap_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("QUANT_LEGACY_SCHEMA_BOOTSTRAP", "false")).strip().lower() in {"1", "true", "yes", "on"}


def provider_global_rate_limit_max_wait_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Keep shared provider reservations bounded so callers fail locally first."""
    env = os.environ if environ is None else environ
    try:
        return min(30.0, max(0.0, float(env.get("QUANT_PROVIDER_GLOBAL_RATE_LIMIT_MAX_WAIT_SECONDS", "5"))))
    except (TypeError, ValueError):
        return 5.0


async def reserve_tushare_provider_request_slot(provider_key: str, rate_limit_per_minute: int,
                                                min_interval_seconds: float) -> None:
    """Reserve a bounded provider start time shared by every service replica."""
    spacing = provider_request_spacing_seconds(rate_limit_per_minute, min_interval_seconds)
    # ``reserve_provider_rate_limit_slot`` deliberately accepts a live SQL
    # connection so its UPSERT and returned start time are atomic.  The async
    # boundary, however, owns a Database.  Keep the transaction opening here
    # rather than passing the Database object into the SQL primitive.
    def reserve() -> float | None:
        with db.transaction() as connection:
            return reserve_provider_rate_limit_slot(
                connection, provider_key, spacing,
                provider_global_rate_limit_max_wait_seconds(),
            )
    wait_seconds = await run_database_blocking(
        reserve, timeout_seconds=5,
    )
    if wait_seconds is None:
        provider_shared_rate_limit_rejections_total.labels(provider_key).inc()
        raise ExecutorSaturatedError(f"shared provider rate-limit queue is full for {provider_key}")
    provider_shared_rate_limit_wait_seconds.labels(provider_key).observe(wait_seconds)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)



def ths_taxonomy_key(index_type: str) -> str:
    return f"ths_index_{index_type.lower()}"


def _normalize_sync_symbols(values: list[str]) -> list[str]:
    """Normalize the already-resolved bounded sync universe."""
    normalized = sorted({value.upper() for value in values if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value.upper())})
    if normalized and "000300.SH" not in normalized:
        normalized.insert(0, "000300.SH")
    return normalized


def resolve_sync_symbols(requested: list[str]) -> list[str]:
    """Synchronous compatibility resolver for non-async callers and tests."""
    values = requested or [item.strip() for item in os.getenv("QUANT_UNIVERSE", "").split(",") if item.strip()]
    if not values:
        with db.transaction() as connection:
            rows = connection.execute(
                "SELECT symbol FROM quant.universe_members WHERE universe_key='core' AND enabled ORDER BY priority,symbol"
            ).fetchall()
        values = [str(row["symbol"]) for row in rows]
    if not values:
        with db.transaction() as connection:
            rows = connection.execute(
                """SELECT DISTINCT subject_key FROM quant.analyst_claims
                   WHERE scope='stock' AND subject_key ~ '^\\d{6}\\.(SH|SZ|BJ)$'"""
            ).fetchall()
        values = [str(row["subject_key"]) for row in rows]
    return _normalize_sync_symbols(values)


async def resolve_sync_symbols_async(requested: list[str]) -> list[str]:
    """Resolve the same bounded universe without blocking an async caller."""
    values = requested or [item.strip() for item in os.getenv("QUANT_UNIVERSE", "").split(",") if item.strip()]
    if not values:
        def load_core() -> list[Any]:
            with db.transaction() as connection:
                return connection.execute(
                    "SELECT symbol FROM quant.universe_members WHERE universe_key='core' AND enabled ORDER BY priority,symbol"
                ).fetchall()
        rows = await run_database_blocking(load_core)
        values = [str(row["symbol"]) for row in rows]
    if not values:
        def load_claims() -> list[Any]:
            with db.transaction() as connection:
                return connection.execute(
                    """SELECT DISTINCT subject_key FROM quant.analyst_claims
                       WHERE scope='stock' AND subject_key ~ '^\\d{6}\\.(SH|SZ|BJ)$'"""
                ).fetchall()
        rows = await run_database_blocking(load_claims)
        values = [str(row["subject_key"]) for row in rows]
    return _normalize_sync_symbols(values)


def baostock_code(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{exchange.lower()}.{code}"


def tushare_daily_api(symbol: str) -> str:
    # Tushare exposes equity and index daily bars through different endpoints.
    # Keep this allow-list explicit; not every 000xxx security is an index.
    return "index_daily" if symbol in {"000300.SH", "000905.SH", "000852.SH"} else "daily"


def ensure_catalog_capabilities() -> None:
    """Register every catalog/provider contract without fabricating verification."""
    items = catalog_items()
    with db.transaction() as connection:
        sync_runtime_provider_rate_limits(connection)
        for item in items:
            contract = api_capability(str(item["api_name"]))
            providers = ["tushare_primary", "tushare_super_sdk"]
            if item["api_name"] in SUPER_GET_VERIFIED_APIS:
                providers.append("tushare_super_get")
            if item["api_name"] == "stock_basic":
                providers.append("tushare_backup")
            for provider_key in providers:
                connection.execute(
                    """INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,metadata)
                       VALUES(%s,%s,'declared',%s,%s,%s,%s)
                       ON CONFLICT(provider_key,api_name) DO UPDATE SET frequency=EXCLUDED.frequency,
                         decision_eligible=EXCLUDED.decision_eligible,
                         metadata=quant.provider_api_capabilities.metadata || EXCLUDED.metadata""",
                    (provider_key, item["api_name"], contract.frequency, contract.decision_eligible,
                     contract.note[:500], Json({
                         "catalog_origin": item["catalog_origin"],
                         "permission_model": item["permission_model"],
                         "min_points": item["min_points"],
                         "request_policy": item["request_policy"],
                         "model_role": item["model_role"],
                         "priority": item["priority"],
                     })),
                )


def sync_runtime_provider_rate_limits(connection: Any, configs: Mapping[str, Any] | None = None) -> None:
    """Mirror the effective Tushare limiter configuration into the read-only control plane.

    Environment configuration is the one runtime source of truth because the
    limiter is process-local and takes effect at startup.  Keeping this small
    mirror current avoids a stale database rate appearing in the UI as if it
    governed live requests; no credentials or endpoint details are stored.
    """
    effective = provider_configs() if configs is None else configs
    for provider in effective.values():
        connection.execute(
            """UPDATE quant.provider_capabilities SET rate_limit_per_minute=%s
                 WHERE provider_key=%s AND market='cn'""",
            (int(provider.rate_limit_per_minute), str(provider.key)),
        )
        connection.execute(
            """UPDATE quant.providers
                  SET config=config || jsonb_build_object(
                        'rate_limit_source','runtime_environment',
                        'runtime_rate_limit_per_minute',%s
                      ),updated_at=now()
                 WHERE provider_key=%s""",
            (int(provider.rate_limit_per_minute), str(provider.key)),
        )


def persist_free_daily(provider: str, rows: list[dict[str, Any]]) -> int:
    """Validate public daily rows first, then persist the valid batch once.

    Public-source daily batches may contain thousands of rows.  Opening one
    pooled transaction per row is still expensive and can starve intraday
    writers; malformed rows are discarded before the single batch transaction.
    """
    # Tencent's public K-line adapter currently requests ``qfq`` (front
    # adjusted) prices.  Canonical daily bars are deliberately unadjusted and
    # use a separately stored adjustment factor, so promoting these rows would
    # silently mix price bases whenever a licensed provider is absent.  Keep
    # Tencent's short-window response as raw, attributable research evidence
    # until an explicitly verified unadjusted adapter exists.
    if provider == "tencent_free":
        return persist_public_observations(provider, "daily_bar", rows)

    valid_bars: list[DailyBar] = []
    for row in rows:
        try:
            trading_date = tushare_date(row.get("trade_date"))
            symbol = str(row.get("ts_code") or "").upper()
            if not trading_date or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise ValueError("free daily row is missing a valid symbol or trading date")
            valid_bars.append(DailyBar(
                symbol=symbol, trading_date=trading_date, close=decimal_or_none(row.get("close")),
                open=decimal_or_none(row.get("open")), high=decimal_or_none(row.get("high")),
                low=decimal_or_none(row.get("low")), volume=decimal_or_none(row.get("vol")),
                amount=decimal_or_none(row.get("amount")), source=provider,
                available_at=datetime.now(timezone.utc),
            ))
        except Exception:
            # The caller records the source status.  A malformed public row
            # must never displace licensed canonical data.
            continue
    if not valid_bars:
        return 0
    with db.transaction() as connection:
        for bar in valid_bars:
            upsert_bar(connection, bar)
    return len(valid_bars)


def persist_free_quote(provider: str, symbol: str, quote: dict[str, Any] | None) -> int:
    """Compatibility entrypoint for the public-evidence repository."""
    return _persist_free_quote(db, provider, symbol, quote)


def persist_free_quotes(provider: str, quotes: list[dict[str, Any]]) -> int:
    """Compatibility entrypoint for the public-evidence repository."""
    return _persist_free_quotes(db, provider, quotes)


def persist_public_observations(provider: str, capability: str, rows: list[dict[str, Any]], symbol: str | None = None) -> int:
    """Compatibility entrypoint for the public-evidence repository."""
    return _persist_public_observations(db, provider, capability, rows, symbol)


def persist_market_events(provider: str, rows: list[dict[str, Any]]) -> int:
    """Compatibility entrypoint for the public-evidence repository."""
    return _persist_market_events(db, provider, rows)


def recent_market_events(symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    """Compatibility entrypoint for the public-evidence repository."""
    return _recent_market_events(db, symbol, limit)


def upsert_bar(connection: Any, bar: DailyBar) -> None:
    """Compatibility entrypoint for existing callers and SQL regression tests."""
    upsert_daily_bar(connection, bar)


def persist_daily_bar_batch(bars: list[DailyBar]) -> int:
    """Persist one validated daily response through a single pooled transaction.

    The controlled per-symbol endpoint can return up to the bounded 45-day
    window.  Opening a database transaction for each returned bar needlessly
    competes with the intraday scan.  A provider response is already one
    atomic evidence unit, so preserve it in one transaction instead.
    """
    if not bars:
        return 0
    with db.transaction() as connection:
        for bar in bars:
            upsert_bar(connection, bar)
    return len(bars)


def run_analysis_job(analysis_id: uuid.UUID) -> dict[str, Any]:
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT a.analysis_id,a.job_id,j.analyst_id,j.source_tag,j.publisher_key,j.created_at,
                      coalesce(i.body,j.payload->>'import_content',j.payload->>'message_text','') AS body
               FROM public.analysis_jobs a
               JOIN public.ingestion_jobs j ON j.job_id=a.job_id
               LEFT JOIN LATERAL (
                 SELECT body FROM public.ingestion_content_items
                 WHERE job_id=j.job_id AND content_type='text' ORDER BY created_at LIMIT 1
               ) i ON true WHERE a.analysis_id=%s""",
            (analysis_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="analysis job not found")
        text = str(row["body"] or "")
        signals = extract_signals(text)
        published_at = as_utc(row["created_at"])
        for signal in signals:
            connection.execute(
                """INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'signal-extraction')
                   ON CONFLICT(symbol) DO NOTHING""",
                (signal.symbol, signal.exchange),
            )
            connection.execute(
                """INSERT INTO quant.analyst_signals(signal_id,ingestion_job_id,analyst_id,source_tag,publisher_key,symbol,direction,
                   strength,horizon_days,published_at,available_at,evidence_text,evidence_offset,extraction_confidence,extractor_version,raw)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(ingestion_job_id,symbol,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
                     strength=EXCLUDED.strength,evidence_text=EXCLUDED.evidence_text,evidence_offset=EXCLUDED.evidence_offset,
                     extraction_confidence=EXCLUDED.extraction_confidence,raw=EXCLUDED.raw""",
                (uuid.uuid4(), row["job_id"], row["analyst_id"], row["source_tag"], row["publisher_key"], signal.symbol,
                 signal.direction, signal.strength, signal.horizon_days, published_at, published_at, signal.evidence_text,
                 signal.evidence_offset, signal.extraction_confidence, EXTRACTOR_VERSION,
                 Json({"method": EXTRACTOR_VERSION})),
            )
    return {
        "kind": "structured-signal-v1",
        "symbols": sorted({signal.symbol for signal in signals}),
        "keywords": keywords(text),
        "signal_count": len(signals),
        "content_length": len(text),
        "extractor_version": EXTRACTOR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def analyst_scorecard_readiness(connection: Any) -> list[dict[str, Any]]:
    """Show why an analyst is or is not allowed to influence a model weight."""
    rows = connection.execute(
        """SELECT a.remote_analyst_id,a.name,
                  count(DISTINCT c.claim_id)::int stock_claims,
                  count(DISTINCT c.claim_id) FILTER (WHERE c.direction<>0)::int directional_stock_claims,
                  count(DISTINCT c.claim_id) FILTER (WHERE c.direction=0)::int neutral_stock_claims,
                  count(DISTINCT o.outcome_id)::int settled_stock_outcomes,
                  max(c.available_at) latest_claim_at
             FROM quant.remote_analysts a
             LEFT JOIN quant.analyst_claims c ON c.remote_analyst_id=a.remote_analyst_id AND c.scope='stock'
             LEFT JOIN quant.outcomes o ON o.claim_id=c.claim_id
             GROUP BY a.remote_analyst_id,a.name
             ORDER BY a.name,a.remote_analyst_id"""
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        directional = int(item["directional_stock_claims"] or 0)
        settled = int(item["settled_stock_outcomes"] or 0)
        if directional == 0:
            reason = "no_directional_stock_claims"
        elif settled < 30:
            reason = "fewer_than_30_settled_stock_outcomes"
        else:
            reason = "eligible_for_scorecard_review"
        result.append({**item, "mature": settled >= 30, "reason": reason})
    return result


def recompute_scorecards_legacy(as_of_date: date | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated scorecard service."""
    return recompute_scorecards(as_of_date)
def recompute_scorecards(as_of_date: date | None = None) -> dict[str, Any]:
    """Compatibility entry point backed by local-only analyst scorecards."""
    return recompute_scorecards_isolated(as_of_date, cn_today=cn_today, db=db, readiness=analyst_scorecard_readiness)


FEATURE_VERSION = "multi-source-feature-v3"
MODEL_VERSION = "multi-source-direction-v1"
ANALYST_TEXT_FACTOR_VERSION = "analyst-text-consensus-v1"


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def bytes_to_gib(value: int | float) -> float:
    """Compatibility export for callers that imported the old helper."""
    return research_capacity.bytes_to_gib(value)


def historical_capacity_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility export for the isolated capacity estimator."""
    return research_capacity.historical_capacity_plan(*args, **kwargs)


def historical_estimate_from_db(request: HistoricalCoverageEstimateRequest) -> dict[str, Any]:
    """Compatibility export backed by the isolated capacity repository."""
    return research_capacity.historical_estimate_from_db(db, request)


def current_data_coverage(connection: Any) -> dict[str, Any]:
    """Compatibility export for the isolated research-capacity projection."""
    return research_capacity.current_data_coverage(connection)


def feature_readiness_state(connection: Any) -> dict[str, Any]:
    """Compatibility export for the isolated research-capacity projection."""
    return research_capacity.feature_readiness_state(connection)


def market_regime(connection: Any, as_of_date: date) -> str:
    """Compatibility export for the isolated feature read repository."""
    return read_market_regime(connection, as_of_date, number)


def latest_tushare_row(connection: Any, api_name: str, symbol: str, as_of_date: date) -> dict[str, Any] | None:
    return read_latest_tushare_row(connection, api_name, symbol, as_of_date)


def analyst_feature(connection: Any, symbol: str, as_of_date: date) -> dict[str, Any]:
    return read_analyst_feature(connection, symbol, as_of_date, number)


def analyst_text_factor_summary(connection: Any, as_of_date: date, lookback_days: int = 7,
                                available_before: datetime | None = None) -> dict[str, Any]:
    """Compatibility export backed by the isolated deterministic aggregator."""
    return read_analyst_text_factor_summary(
        connection, as_of_date, classify_text=classify_remote_text,
        factor_version=ANALYST_TEXT_FACTOR_VERSION, lookback_days=lookback_days,
        available_before=available_before,
    )


def build_feature_snapshot(as_of_date: date, universe_key: str = "core") -> dict[str, Any]:
    """Materialize deterministic, source-labelled features for the active universe."""
    with db.transaction() as connection:
        try:
            return materialize_feature_snapshot(
                connection, as_of_date, universe_key, feature_version=FEATURE_VERSION, number=number,
                market_regime=market_regime, analyst_text_factor_summary=analyst_text_factor_summary,
                latest_tushare_row=latest_tushare_row, analyst_feature=analyst_feature,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


def intraday_market_context_from_board_report(row: Any, observed_at: datetime,
                                              symbol: str | None = None) -> dict[str, Any]:
    """Describe a signal using one already-selected, point-in-time board report."""
    if row is None:
        return {"status": "missing", "market_state": "unknown", "board_snapshot_age_seconds": None,
                "symbol_board_matches": [], "notice": "no board snapshot existed before the signal"}
    items = list((row["payload"] or {}).get("items") or [])
    market_state, metrics = strategy_market_state(items) if items else ("unknown", {"known_board_flows": 0})
    matches: list[dict[str, Any]] = []
    if symbol:
        for item in items:
            if any(str(stock.get("symbol") or "") == symbol for stock in item.get("top_stocks") or []):
                matches.append({key: item.get(key) for key in
                                ("taxonomy_key", "sector_key", "label", "net_inflow", "change_pct")})
    matches.sort(key=lambda item: float(intraday_number(item.get("net_inflow")) or -math.inf), reverse=True)
    return {"status": "available", "board_report_id": str(row["board_report_id"]),
            "board_observed_at": row["observed_at"].isoformat(),
            "board_snapshot_age_seconds": round(max(0.0, (observed_at - row["observed_at"]).total_seconds()), 1),
            "market_state": market_state, "market_state_metrics": metrics,
            "symbol_board_matches": matches[:8],
            "match_semantics": "saved board Top10 occurrence; not full membership coverage"}


def intraday_point_in_time_market_context(connection: Any, observed_at: datetime,
                                          symbol: str | None = None) -> dict[str, Any]:
    """Describe only the latest board snapshot known when a signal fired."""
    row = connection.execute(
        """SELECT board_report_id,observed_at,payload FROM quant.intraday_board_reports
             WHERE status='completed' AND observed_at<=%s ORDER BY observed_at DESC LIMIT 1""",
        (observed_at,),
    ).fetchone()
    return intraday_market_context_from_board_report(row, observed_at, symbol)


def intraday_point_in_time_market_context_batch(
    connection: Any, observations: list[tuple[datetime, str]],
) -> dict[tuple[datetime, str], dict[str, Any]]:
    """Resolve point-in-time board context with one bounded report query.

    The outcome API may contain several horizons per signal.  Fetching a board
    report per row creates an N+1 read pattern; the report immediately before
    the earliest signal plus all reports through the latest signal is enough to
    reproduce the same "latest report at or before signal time" rule.
    """
    normalized = [(observed_at, str(symbol)) for observed_at, symbol in observations if isinstance(observed_at, datetime)]
    if not normalized:
        return {}
    earliest, latest = min(item[0] for item in normalized), max(item[0] for item in normalized)
    rows = connection.execute(
        """SELECT board_report_id,observed_at,payload FROM quant.intraday_board_reports
             WHERE status='completed' AND observed_at<=%s
               AND (observed_at>=%s OR observed_at=(
                   SELECT max(observed_at) FROM quant.intraday_board_reports
                    WHERE status='completed' AND observed_at<%s
               ))
             ORDER BY observed_at""",
        (latest, earliest, earliest),
    ).fetchall()
    reports = [dict(row) for row in rows]
    report_times = [row["observed_at"] for row in reports]
    contexts: dict[tuple[datetime, str], dict[str, Any]] = {}
    for observed_at, symbol in normalized:
        position = bisect_right(report_times, observed_at) - 1
        report = reports[position] if position >= 0 else None
        contexts[(observed_at, symbol)] = intraday_market_context_from_board_report(report, observed_at, symbol)
    return contexts


def intraday_signal_attribution(signal_key: str, signal_type: str,
                                conditions: dict[str, Any] | None,
                                evidence: dict[str, Any] | None,
                                market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compatibility export backed by the pure attribution labeler."""
    return pure_signal_attribution(
        signal_key, signal_type, conditions, evidence, market_context,
        number=intraday_number, signal_model_version=INTRADAY_SIGNAL_MODEL_VERSION,
    )


def intraday_outcome_attribution_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Compatibility export backed by pure cohort aggregation."""
    return pure_outcome_attribution_summary(items, number=intraday_number)


def refresh_intraday_signal_attributions(connection: Any, *, cutoff: datetime) -> int:
    """Backfill deterministic attribution after a classifier correction.

    Signal evidence is immutable, but attribution is a derived research label.
    Rebuilding it in the same transaction as outcome settlement prevents old
    EAC labels from contaminating subsequent offline policy reviews.
    """
    rows = connection.execute(
        """SELECT signal_event_id,signal_key,signal_type,conditions,evidence
             FROM quant.intraday_signal_events WHERE observed_at<=%s""",
        (cutoff,),
    ).fetchall()
    changed = 0
    for row in rows:
        evidence = dict(row["evidence"] or {})
        attribution = intraday_signal_attribution(
            str(row["signal_key"]), str(row["signal_type"]),
            dict(row["conditions"] or {}), evidence,
        )
        if evidence.get("attribution") == attribution:
            continue
        evidence["attribution"] = attribution
        connection.execute(
            "UPDATE quant.intraday_signal_events SET evidence=%s WHERE signal_event_id=%s",
            (Json(strategy_json_safe(evidence)), row["signal_event_id"]),
        )
        changed += 1
    return changed


def recompute_intraday_signal_outcomes_legacy(as_of_date: date | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated outcome service."""
    return recompute_intraday_signal_outcomes(as_of_date)
def recompute_intraday_signal_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    """Settle confirmed alerts from persisted evidence through the shared repository."""
    cutoff = intraday_outcome_cutoff(as_of_date)
    with db.transaction() as connection:
        attribution_backfilled = refresh_intraday_signal_attributions(connection, cutoff=cutoff)
        result = persist_intraday_outcome_settlement(
            connection, as_of_date, cutoff=cutoff, horizons=INTRADAY_OUTCOME_HORIZONS,
            direction_for=intraday_signal_direction, metrics_for=intraday_signal_outcome_metrics,
            decimal_or_none=decimal_or_none, barrier_spec_type=LabelSpec,
            triple_barrier_label=triple_barrier_label, persist_barrier_outcome=persist_barrier_outcome,
            return_decomposition=a_share_return_decomposition, json_safe=strategy_json_safe,
        )
    invalidate_intraday_probability_profiles()
    return {**result, "attribution_backfilled": attribution_backfilled}


def recompute_outcomes_legacy(as_of_date: date | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated outcome service."""
    return recompute_outcomes(as_of_date)
def recompute_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    """Compatibility entry point backed by local-only outcome recomputation."""
    return recompute_outcomes_isolated(
        as_of_date,
        cn_today=cn_today,
        db=db,
        recompute_intraday_signal_outcomes=recompute_intraday_signal_outcomes,
    )


def generate_recommendations_legacy(request: GenerateRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated recommendation service."""
    return generate_recommendations(request)
def generate_recommendations(request: GenerateRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated scorer/materializer."""
    return generate_recommendations_isolated(
        request, cn_today=cn_today, build_feature_snapshot=build_feature_snapshot,
        analyst_execution_context=analyst_execution_context, ablation_scores=ablation_scores,
        number=number, db=db, model_version=MODEL_VERSION, feature_version=FEATURE_VERSION,
        json_safe=strategy_json_safe,
    )


async def sync_tushare_legacy(request: TushareSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated synchronizer."""
    return await sync_tushare(request)

async def sync_tushare(request: TushareSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated daily synchronizer."""
    return await sync_tushare_isolated(
        request,
        resolve_symbols=resolve_sync_symbols_async,
        provider_candidates=provider_candidates,
        cn_today=cn_today,
        tushare_daily_api=tushare_daily_api,
        call_tushare_api=call_tushare_api,
        decimal_or_none=decimal_or_none,
        daily_bar_type=DailyBar,
        persist_daily_bar_batch=persist_daily_bar_batch,
        run_database_blocking=run_database_blocking,
        db=db,
        record_provider_failure=record_provider_failure,
        record_provider_success=record_provider_success,
        safe_error_detail=safe_error_detail,
        executor_saturated_error=ExecutorSaturatedError,
    )


def fetch_baostock_rows_legacy(symbols: list[str], trade_date: date) -> tuple[list[dict[str, str]], list[str]]:
    """Deprecated compatibility alias; use the isolated BaoStock fetcher."""
    return fetch_baostock_rows_isolated(symbols, trade_date, baostock_code=baostock_code)


async def sync_baostock_legacy(request: TushareSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated BaoStock synchronizer."""
    return await sync_baostock(request)

async def sync_baostock(request: TushareSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated BaoStock synchronizer."""
    return await sync_baostock_isolated(
        request,
        resolve_symbols=resolve_sync_symbols_async,
        cn_today=cn_today,
        open_provider_capabilities=open_provider_capabilities,
        run_database_blocking=run_database_blocking,
        run_public_blocking=run_akshare_blocking,
        fetch_rows=fetch_baostock_rows_isolated,
        baostock_code=baostock_code,
        daily_bar_type=DailyBar,
        decimal_or_none=decimal_or_none,
        persist_daily_bar_batch=persist_daily_bar_batch,
        db=db,
        safe_error_detail=safe_error_detail,
        record_provider_failure=record_provider_failure,
        record_provider_success=record_provider_success,
        executor_saturated_error=ExecutorSaturatedError,
    )


async def call_tushare_api(api_name: str, params: dict[str, Any], fields: str | None,
                           provider: ProviderPreference = "auto", *, paginate: bool = False,
                           page_size: int = 1000, max_rows: int = 10_000,
                           max_pages: int = 20, require_complete: bool = False,
                           blocked_provider_keys: set[str] | None = None) -> Any:
    """Invoke an allow-listed API through the configured provider fallback order."""
    if blocked_provider_keys is None:
        candidates = provider_candidates(api_name, provider)
        blocked_provider_keys = await circuit_open_provider_keys_async(api_name, candidates)
    return await call_with_fallback(
        api_name, params, fields, provider, paginate=paginate,
        page_size=page_size, max_rows=max_rows, max_pages=max_pages,
        require_complete=require_complete, blocked_provider_keys=blocked_provider_keys,
    )


async def circuit_open_provider_keys_async(capability: str, candidates: list[Any]) -> set[str]:
    """Async-loop-safe provider circuit lookup for generic catalog calls."""
    keys = [item.key for item in candidates]
    if not keys:
        return set()

    def load() -> list[Any]:
        with db.transaction() as connection:
            return connection.execute(
                """SELECT provider_key FROM quant.provider_health
                     WHERE capability=%s AND market='cn' AND provider_key=ANY(%s)
                       AND circuit_open_until IS NOT NULL AND circuit_open_until > now()""",
                (capability, keys),
            ).fetchall()

    rows = await run_database_blocking(load)
    return {str(row["provider_key"]) for row in rows}


def tushare_record_key(row: dict[str, Any], request_key: str, index: int) -> str:
    key_fields = ("ts_code", "trade_date", "cal_date", "ann_date", "end_date", "exchange", "index_code", "con_code", "name")
    values = [f"{name}={row[name]}" for name in key_fields if row.get(name) not in (None, "")]
    return "|".join(values) if values else f"{request_key}:{index}"


def tushare_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    text = str(value)
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def ensure_tushare_instrument(connection: Any, symbol: str) -> None:
    connection.execute(
        "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'tushare') ON CONFLICT(symbol) DO NOTHING",
        (symbol, exchange_for(symbol)),
    )


def offline_data_root() -> Path:
    """Return the sole directory from which offline imports may be read."""
    root = Path(os.getenv("OFFLINE_DATA_DIR", "/var/lib/quant/offline")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def offline_import_path(file_name: str) -> Path:
    root = offline_data_root()
    path = (root / file_name).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("offline CSV file does not exist in the configured offline directory")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def offline_minute_timestamp(value: Any) -> datetime:
    """Parse vendor local timestamps; naive input is Shanghai exchange time."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("minute row has no datetime")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("datetime must be ISO-8601 or YYYY-MM-DD HH:MM:SS") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


def offline_minute_source_available_at(row: dict[str, Any]) -> datetime | None:
    """Return a vendor-recorded availability clock without manufacturing one.

    ``bar_time`` says when a bar closed, not when a caller could have seen it.
    CSV producers may provide an explicit source/provider availability or
    receive timestamp.  Missing or blank values intentionally remain NULL so
    the file cannot be admitted to causal strategy replay by accident.
    """
    for key in ("source_available_at", "provider_available_at", "received_at", "available_at"):
        value = row.get(key)
        if value not in (None, ""):
            return offline_minute_timestamp(value)
    return None


def offline_minute_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate one CSV row before it reaches the database."""
    symbol = str(row.get("ts_code") or row.get("symbol") or "").upper().strip()
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise ValueError("minute row needs ts_code or symbol in 000001.SZ form")
    bar_time = offline_minute_timestamp(row.get("datetime") or row.get("bar_time") or row.get("time"))
    open_price = decimal_or_none(row.get("open"))
    high = decimal_or_none(row.get("high"))
    low = decimal_or_none(row.get("low"))
    close = decimal_or_none(row.get("close"))
    if any(value is None for value in (open_price, high, low, close)) or min(open_price, high, low, close) <= 0:
        raise ValueError("minute row needs positive open, high, low and close")
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        raise ValueError("minute OHLC values are inconsistent")
    volume = decimal_or_none(row.get("volume") if row.get("volume") not in (None, "") else row.get("vol"))
    amount = decimal_or_none(row.get("amount"))
    if volume is not None and volume < 0 or amount is not None and amount < 0:
        raise ValueError("minute volume and amount must not be negative")
    return {"symbol": symbol, "bar_time": bar_time, "open": open_price, "high": high, "low": low,
            "close": close, "volume": volume, "amount": amount,
            "source_available_at": offline_minute_source_available_at(row), "raw": row}


def ensure_offline_instrument(connection: Any, symbol: str) -> None:
    connection.execute(
        "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'offline-import') ON CONFLICT(symbol) DO NOTHING",
        (symbol, exchange_for(symbol)),
    )


def import_offline_minute_csv(request: OfflineMinuteImportRequest) -> dict[str, Any]:
    """Stream a locally mounted minute CSV into PostgreSQL in bounded batches."""
    path = offline_import_path(request.file_name)
    file_sha256 = sha256_file(path)
    with db.transaction() as connection:
        existing = connection.execute(
            "SELECT import_id,status,row_count,rejected_rows FROM quant.offline_imports WHERE file_sha256=%s", (file_sha256,)
        ).fetchone()
        if existing:
            return {"status": "unchanged", "import_id": str(existing["import_id"]), "stored": existing["row_count"],
                    "rejected_rows": existing["rejected_rows"], "file_name": request.file_name}
        import_id = connection.execute(
            """INSERT INTO quant.offline_imports(source_name,file_name,file_sha256,dataset_kind,status)
               VALUES(%s,%s,%s,'minute_bar','running') RETURNING import_id""",
            (request.source_name, request.file_name, file_sha256),
        ).fetchone()["import_id"]

    accepted = 0
    rejected = 0
    batch: list[dict[str, Any]] = []

    def write_batch(items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with db.transaction() as connection:
            for item in items:
                ensure_offline_instrument(connection, item["symbol"])
                connection.execute(
                    """INSERT INTO quant.market_bars_minute(symbol,bar_time,open,high,low,close,volume,amount,source_name,import_id,source_available_at,available_at,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
                       ON CONFLICT(symbol,bar_time,source_name) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                         close=EXCLUDED.close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,import_id=EXCLUDED.import_id,
                         source_available_at=EXCLUDED.source_available_at,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                    (item["symbol"], item["bar_time"], item["open"], item["high"], item["low"], item["close"], item["volume"],
                     item["amount"], request.source_name, import_id, item["source_available_at"], Json(item["raw"])),
                )

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError("offline CSV needs a header row")
            for line_number, row in enumerate(reader, start=2):
                if line_number - 1 > request.max_rows:
                    raise ValueError(f"offline CSV exceeds the {request.max_rows} row safety cap")
                try:
                    batch.append(offline_minute_row(dict(row)))
                    accepted += 1
                except (ValueError, ArithmeticError):
                    rejected += 1
                if len(batch) >= 1000:
                    write_batch(batch)
                    batch.clear()
            write_batch(batch)
        status = "completed" if rejected == 0 else "partial"
        with db.transaction() as connection:
            connection.execute(
                """UPDATE quant.offline_imports SET status=%s,row_count=%s,rejected_rows=%s,finished_at=now() WHERE import_id=%s""",
                (status, accepted, rejected, import_id),
            )
        return {"status": status, "import_id": str(import_id), "stored": accepted, "rejected_rows": rejected,
                "file_name": request.file_name, "file_sha256": file_sha256}
    except Exception as error:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE quant.offline_imports SET status='failed',row_count=%s,rejected_rows=%s,error_message=%s,finished_at=now() WHERE import_id=%s",
                (accepted, rejected, safe_error_detail(str(error), 1000), import_id),
            )
        raise


def normalize_tushare_rows(connection: Any, api_name: str, rows: list[dict[str, Any]], available_at: datetime,
                           provider_key: str = "tushare") -> int:
    """Compatibility export backed by the isolated Tushare normalizer."""
    return pure_normalize_tushare_rows(
        connection, api_name, rows, available_at,
        core_apis=CORE_NORMALIZED_APIS, date_parser=tushare_date, exchange_for=exchange_for,
        is_st_security_name=is_st_security_name, ensure_instrument=ensure_tushare_instrument,
        upsert_bar=upsert_bar, daily_bar_type=DailyBar, decimal_or_none=decimal_or_none,
        safe_error_detail=safe_error_detail, provider_key=provider_key,
    )

def persist_tushare_rows(connection: Any, api_name: str, request_key: str, rows: list[dict[str, Any]],
                         provider_key: str, available_at: datetime) -> int:
    """Persist raw API evidence before promoting the supported canonical subset."""
    for index, row in enumerate(rows):
        serialized = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        connection.execute(
            """INSERT INTO quant.tushare_raw_records(provider_key,api_name,request_key,record_index,record_key,content_sha256,row_data,available_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(provider_key,api_name,record_key,content_sha256) DO UPDATE SET available_at=EXCLUDED.available_at,request_key=EXCLUDED.request_key""",
            (provider_key, api_name, request_key, index, tushare_record_key(row, request_key, index),
             hashlib.sha256(serialized.encode()).hexdigest(), Json(row), available_at),
        )
    return normalize_tushare_rows(connection, api_name, rows, available_at, provider_key)


async def sync_market_universe_legacy(request: MarketUniverseSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated universe synchronizer."""
    return await sync_market_universe(request)

async def sync_market_universe(request: MarketUniverseSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated universe synchronizer."""
    return await sync_market_universe_isolated(
        request,
        provider_candidates=provider_candidates,
        cn_date=cn_today,
        call_tushare_api=call_tushare_api,
        looks_like_response_header=looks_like_response_header,
        persist_tushare_rows=persist_tushare_rows,
        run_database_blocking=run_database_blocking,
        persist_tushare_fetch_blocked=persist_tushare_fetch_blocked,
        db=db,
        safe_error_detail=safe_error_detail,
        provider_call_error=ProviderCallError,
        executor_saturated_error=ExecutorSaturatedError,
        record_provider_success=record_provider_success,
        record_provider_failure=record_provider_failure,
        record_provider_api_capability=record_provider_api_capability,
    )


async def sync_full_market_daily_legacy(request: FullMarketDailySyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated full-market synchronizer."""
    return await sync_full_market_daily(request)

async def sync_full_market_daily(request: FullMarketDailySyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated full-market sync."""
    return await sync_full_market_daily_isolated(
        request,
        provider_candidates=provider_candidates,
        cn_date=cn_today,
        call_tushare_api=call_tushare_api,
        looks_like_response_header=looks_like_response_header,
        tushare_date=tushare_date,
        persist_tushare_rows=persist_tushare_rows,
        run_database_blocking=run_database_blocking,
        persist_tushare_fetch_blocked=persist_tushare_fetch_blocked,
        db=db,
        safe_error_detail=safe_error_detail,
        provider_call_error=ProviderCallError,
        executor_saturated_error=ExecutorSaturatedError,
        record_provider_success=record_provider_success,
        record_provider_failure=record_provider_failure,
        record_provider_api_capability=record_provider_api_capability,
    )


def upsert_sector_taxonomy(connection: Any, taxonomy_key: str, label: str, provider_key: str, metadata: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO quant.sector_taxonomies(taxonomy_key,label,provider_key,metadata)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(taxonomy_key) DO UPDATE SET label=EXCLUDED.label,provider_key=EXCLUDED.provider_key,
             metadata=EXCLUDED.metadata,updated_at=now()""",
        (taxonomy_key, label, provider_key, Json(metadata)),
    )


def upsert_sector(connection: Any, taxonomy_key: str, sector_key: str, label: str, metadata: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO quant.sectors(taxonomy_key,sector_key,label,metadata)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT(taxonomy_key,sector_key) DO UPDATE SET label=EXCLUDED.label,metadata=EXCLUDED.metadata,updated_at=now()""",
        (taxonomy_key, sector_key, label, Json(metadata)),
    )


def persist_ths_sector_members(connection: Any, taxonomy_key: str, sector_key: str, rows: list[dict[str, Any]],
                               provider_key: str, available_at: datetime) -> int:
    """Apply one complete constituent response as point-in-time membership evidence."""
    sync_date = available_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    active_members: set[str] = set()
    for row in rows:
        symbol = str(row.get("con_code") or "").upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            continue
        ensure_tushare_instrument(connection, symbol)
        effective_from = tushare_date(row.get("in_date")) or date(1900, 1, 1)
        effective_to = tushare_date(row.get("out_date"))
        connection.execute(
            """INSERT INTO quant.sector_membership_history(taxonomy_key,sector_key,symbol,effective_from,effective_to,provider_key,available_at,raw)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(taxonomy_key,sector_key,symbol,effective_from) DO UPDATE SET effective_to=EXCLUDED.effective_to,
                 provider_key=EXCLUDED.provider_key,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (taxonomy_key, sector_key, symbol, effective_from, effective_to, provider_key, available_at, Json(row)),
        )
        # THS can return historical constituents together with the current
        # snapshot.  Only open membership rows belong in today's Top10
        # denominator; counting every evidence row materially overstates the
        # live board size.
        if effective_to is None:
            active_members.add(symbol)
    # A successful response is authoritative for current constituents. Keep
    # prior history but close only rows that are still open and no longer seen.
    if rows:
        connection.execute(
            """UPDATE quant.sector_membership_history SET effective_to=%s,available_at=%s
                 WHERE taxonomy_key=%s AND sector_key=%s AND effective_to IS NULL
                   AND NOT symbol = ANY(%s)""",
            (sync_date - timedelta(days=1), available_at, taxonomy_key, sector_key, list(active_members)),
        )
    return len(active_members)


def eastmoney_member_symbol(row: dict[str, Any]) -> str | None:
    """Normalize the public board constituent code without guessing exchanges."""
    code = str(row.get("代码") or row.get("code") or row.get("股票代码") or "").strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        return code
    if not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    return None


def persist_eastmoney_sector_members(connection: Any, taxonomy_key: str, sector_key: str, rows: list[dict[str, Any]],
                                     available_at: datetime) -> int:
    """Apply one complete public Eastmoney board member response as a snapshot."""
    members: set[str] = set()
    stored = 0
    for row in rows:
        symbol = eastmoney_member_symbol(row)
        if not symbol:
            continue
        connection.execute(
            "INSERT INTO quant.instruments(symbol,exchange,name,source) VALUES(%s,%s,%s,'akshare') "
            "ON CONFLICT(symbol) DO UPDATE SET name=coalesce(EXCLUDED.name,quant.instruments.name),updated_at=now()",
            (symbol, exchange_for(symbol), str(row.get("名称") or row.get("name") or "").strip() or None),
        )
        connection.execute(
            """INSERT INTO quant.sector_membership_history(taxonomy_key,sector_key,symbol,effective_from,effective_to,provider_key,available_at,raw)
               VALUES(%s,%s,%s,'1900-01-01',null,'akshare',%s,%s)
               ON CONFLICT(taxonomy_key,sector_key,symbol,effective_from) DO UPDATE SET effective_to=null,
                 provider_key=EXCLUDED.provider_key,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
            (taxonomy_key, sector_key, symbol, available_at, Json(row)),
        )
        members.add(symbol)
        stored += 1
    if members:
        sync_date = available_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        connection.execute(
            """UPDATE quant.sector_membership_history SET effective_to=%s,available_at=%s
                 WHERE taxonomy_key=%s AND sector_key=%s AND provider_key='akshare' AND effective_to IS NULL
                   AND NOT symbol = ANY(%s)""",
            (sync_date - timedelta(days=1), available_at, taxonomy_key, sector_key, list(members)),
        )
    return stored


async def sync_ths_sector_catalog_legacy(request: SectorCatalogSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated THS catalog synchronizer."""
    return await sync_ths_sector_catalog(request)

async def sync_ths_sector_catalog(request: SectorCatalogSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated THS catalog sync."""
    # The isolated module keeps the exact member-code guard: re.fullmatch(r"\d{6}\.TI", code)
    # and returns skipped_non_member_codes for audit visibility.
    return await sync_ths_sector_catalog_isolated(
        request,
        taxonomy_key=ths_taxonomy_key,
        fetch_catalog=fetch_tushare_catalog,
        catalog_request=TushareFetchRequest,
        load_rows=lambda request_key: run_database_blocking(tushare_rows_for_request, request_key),
        run_database_blocking=run_database_blocking,
        db=db,
        upsert_taxonomy=upsert_sector_taxonomy,
        upsert_sector=upsert_sector,
        ths_member_persist=persist_ths_sector_members,
        member_sync_failure=record_sector_member_sync_failure,
        is_local_capacity_error=is_local_capacity_http_error,
        is_circuit_open_error=is_circuit_open_http_error,
        http_exception=HTTPException,
        observed_at=lambda: datetime.now(timezone.utc),
    )


async def sync_all_ths_sector_catalogs() -> dict[str, Any]:
    """Compatibility entry point backed by bounded catalog orchestration."""
    return await sync_all_sector_catalogs_isolated(
        sync_one=sync_ths_sector_catalog,
        request_type=SectorCatalogSyncRequest,
        http_exception=HTTPException,
        is_local_capacity_error=is_local_capacity_http_error,
        is_circuit_open_error=is_circuit_open_http_error,
    )


async def sync_eastmoney_board_members_legacy(request: EastmoneyBoardMemberSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated Eastmoney member synchronizer."""
    return await sync_eastmoney_board_members(request)

async def sync_eastmoney_board_members(request: EastmoneyBoardMemberSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated Eastmoney member sync."""
    return await sync_eastmoney_sector_members_isolated(
        request,
        board_catalog=akshare_eastmoney_board_catalog,
        board_members=akshare_eastmoney_board_members,
        run_public_blocking=run_akshare_blocking,
        run_database_blocking=run_database_blocking,
        db=db,
        upsert_taxonomy=upsert_sector_taxonomy,
        upsert_sector=upsert_sector,
        persist_members=persist_eastmoney_sector_members,
        record_failure=record_sector_member_sync_failure,
        safe_error_detail=safe_error_detail,
        executor_saturated_error=ExecutorSaturatedError,
        provider_error=AkShareProviderError,
        observed_at=datetime.now(timezone.utc),
    )


async def record_sector_member_sync_failure(taxonomy_key: str, sector_key: str, observed_at: datetime,
                                             detail: str, provider_key: str) -> None:
    """Record a retry-bounded member failure without closing prior members."""
    def persist() -> None:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,provider_key,updated_at)
                   VALUES(%s,%s,%s,'failed',1,0,%s,%s,now())
                   ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state='failed',
                     attempts=quant.sector_member_sync_state.attempts+1,last_error=EXCLUDED.last_error,
                     provider_key=EXCLUDED.provider_key,updated_at=now()""",
                (taxonomy_key, sector_key, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date(), detail, provider_key),
            )
    await run_database_blocking(persist)


async def hydrate_eastmoney_live_board_members(kind: str, flows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Hydrate only the strongest unmapped live boards using their exact EM name.

    The EM directory is intermittently unavailable, while the live-flow row
    still provides an upstream board name that the member endpoint accepts.
    This bounded path avoids an all-board scrape and writes only exact same-
    source memberships under the live board code.
    """
    return await hydrate_eastmoney_live_isolated(
        kind, flows, limit,
        run_database_blocking=run_database_blocking,
        run_public_blocking=run_akshare_blocking,
        board_members=akshare_eastmoney_board_members,
        upsert_taxonomy=upsert_sector_taxonomy,
        upsert_sector=upsert_sector,
        persist_members=persist_eastmoney_sector_members,
        db=db,
        intraday_number=intraday_number,
        executor_saturated_error=ExecutorSaturatedError,
        provider_error=AkShareProviderError,
        safe_error_detail=safe_error_detail,
    )
def ths_concept_top_stocks(flow_rows: list[dict[str, Any]], member_rows: list[dict[str, Any]],
                           quotes: dict[str, dict[str, Any]], top_stocks: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Join Tushare concept flows and members by their common THS ``ts_code``.

    Display names are never used as a cross-source membership key.  Tencent is
    only the intraday stock-ranking cross-section after the exact THS join.
    """
    members_by_sector: dict[str, list[str]] = {}
    for row in member_rows:
        sector_key, symbol = str(row.get("sector_key") or ""), str(row.get("symbol") or "")
        if sector_key and symbol:
            members_by_sector.setdefault(sector_key, []).append(symbol)
    items: list[dict[str, Any]] = []
    mapped_boards = 0
    quoted_members = 0
    for flow in flow_rows:
        sector_key = str(flow.get("sector_key") or "")
        members = members_by_sector.get(sector_key, [])
        stocks = [quotes[symbol] for symbol in members if symbol in quotes]
        stocks.sort(key=lambda item: (item.get("main_net_inflow") is None, -(item.get("main_net_inflow") or 0), -(item.get("turnover") or 0)))
        mapped_boards += int(bool(members))
        quoted_members += len(stocks)
        items.append({"taxonomy_key": "ths_concept_flow", "sector_key": sector_key,
                      "label": flow.get("label") or sector_key, "net_inflow": intraday_number(flow.get("net_amount")),
                      "change_pct": intraday_number(flow.get("change_pct")), "mapped_members": len(members),
                      "quoted_members": len(stocks), "top_stocks": stocks[:top_stocks], "member_quotes": stocks,
                      "trade_date": str(flow.get("trading_date") or "")})
    return items, {"flow_boards": len(flow_rows), "boards_with_members": mapped_boards, "quoted_members": quoted_members}


def build_intraday_sector_report_from_membership(
    kinds: tuple[str, ...],
    flow_parts: list[list[dict[str, Any]]],
    quotes: dict[str, dict[str, Any]],
    top_stocks: int,
    exchange_date: date,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[Any], list[Any], list[Any]]:
    """Compatibility wrapper around the isolated point-in-time SQL join."""
    return build_intraday_sector_report_from_membership_isolated(
        db, kinds, flow_parts, quotes, top_stocks, exchange_date,
        number=intraday_number, ths_top_stocks=ths_concept_top_stocks,
    )


async def intraday_sector_report(request: IntradaySectorReportRequest) -> dict[str, Any]:
    """Return same-source board flow with per-board Tencent main-flow leaders."""
    result = await run_intraday_sector_report_isolated(
        request,
        run_public_blocking=run_akshare_blocking,
        board_flow=akshare_eastmoney_board_flow,
        all_a_spot=akshare_tencent_all_a_spot,
        build_membership_report=lambda kinds, flows, quotes, top_n, exchange_date: run_database_blocking(
            build_intraday_sector_report_from_membership, kinds, flows, quotes, top_n, exchange_date,
        ),
        hydrate_members=hydrate_eastmoney_live_board_members,
        member_symbol=eastmoney_member_symbol,
        number=intraday_number,
        exchange_date=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date(),
        safe_error=safe_error_detail,
        executor_saturated_error=ExecutorSaturatedError,
        provider_error=AkShareProviderError,
    )
    return {"observed_at": datetime.now(timezone.utc).isoformat(), **result}


INTRADAY_SIGNAL_MODEL_VERSION = "watchlist-confirmation-v5"
INTRADAY_CONFIRMATION_WINDOW = timedelta(minutes=5)
INTRADAY_ALERT_COOLDOWN = timedelta(minutes=10)
INTRADAY_ALERT_MAX_ATTEMPTS = 3
# This process-local cache contains only the current explicit watch/peer
# basket.  Entries expire quickly and are pruned in ``intraday_tencent_surge_context``.
_intraday_tencent_minute_cache: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}
INTRADAY_ALL_A_SNAPSHOT_TTL_SECONDS = 30.0


async def _fetch_intraday_all_a_snapshot_rows() -> list[dict[str, Any]]:
    """Fetch the broad Tencent cross section in its bounded provider executor."""
    return await run_akshare_blocking(akshare_tencent_all_a_spot, timeout_seconds=20)


_intraday_all_a_snapshots = SharedAsyncSnapshot(
    _fetch_intraday_all_a_snapshot_rows,
    ttl_seconds=INTRADAY_ALL_A_SNAPSHOT_TTL_SECONDS,
    clock=lambda: asyncio.get_running_loop().time(),
)


def consume_background_task_exception(task: asyncio.Task[Any]) -> None:
    """Observe a detached task failure without changing await semantics.

    A watch scan has a two-second budget for the optional all-A percentile
    snapshot.  That task is intentionally allowed to finish in the background;
    consuming an eventual exception prevents an unobserved-task warning while
    a later scan may still await the same shared task normally.
    """
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


async def intraday_all_a_snapshot() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return one shared, explicitly aged all-A flow cross-section.

    It is used only for cross-sectional flow percentiles. Per-watch prices
    are refreshed independently from Tencent's batched quote path, so a slow
    all-A response cannot make a 10-second watch scan pretend its price is
    fresh. The age is carried into the scan evidence.
    """
    return await _intraday_all_a_snapshots.get()


def merge_intraday_watch_quote_prices(quotes: dict[str, dict[str, Any]], depth_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Overlay fresh batched watch prices without inventing flow fields."""
    for row in depth_rows:
        symbol = str(row.get("ts_code") or "")
        price, pre_close = intraday_number(row.get("price")), intraday_number(row.get("pre_close"))
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or price is None or price <= 0:
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        existing["price"] = price
        existing["pct_change"] = round((price / pre_close - 1) * 100, 5) if pre_close and pre_close > 0 else existing.get("pct_change")
        existing["price_source"] = "tencent_batched_watch_quote"
        existing["price_observed_from_depth"] = True
        existing["price_trade_time"] = row.get("trade_time")
        existing["raw"] = {**(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}), "watch_quote": row}
        quotes[symbol] = existing
    return quotes


def merge_intraday_sina_watch_quotes(quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Use Sina only as a price fallback; do not invent Tencent flow fields."""
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        price, pre_close = intraday_number(row.get("close")), intraday_number(row.get("pre_close"))
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or price is None or price <= 0:
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        existing["price"] = price
        existing["pct_change"] = round((price / pre_close - 1) * 100, 5) if pre_close and pre_close > 0 else existing.get("pct_change")
        existing["price_source"] = "sina_batched_watch_quote"
        existing["price_trade_date"] = row.get("trade_date")
        existing["price_trade_time"] = row.get("trade_time")
        existing["raw"] = {**(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}), "sina_watch_quote": row}
        quotes[symbol] = existing
    return quotes


def merge_intraday_eastmoney_watch_flows(quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Overlay bounded Eastmoney watch-basket flow without changing price source.

    The direct Tencent depth batch remains the only decision-eligible price
    confirmation.  Eastmoney here only fills same-scan flow/turnover features
    after the all-A Tencent percentile snapshot is unavailable.
    """
    for row in rows:
        symbol = str(row.get("ts_code") or "")
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            continue
        existing = dict(quotes.get(symbol) or {"symbol": symbol, "name": row.get("name"), "raw": {}})
        for key in ("volume_ratio", "turnover_rate", "main_net_inflow", "main_net_inflow_ratio"):
            value = intraday_number(row.get(key))
            if value is not None:
                existing[key] = value
        existing["main_flow_percentile"] = None
        existing["raw"] = {**(existing.get("raw") if isinstance(existing.get("raw"), dict) else {}),
                           "eastmoney_watch_flow": row.get("raw") if isinstance(row.get("raw"), dict) else row}
        quotes[symbol] = existing
    return quotes


def intraday_quote_observation_source(quote: dict[str, Any] | None) -> str:
    """Return the actual provider used for one persisted watch-price frame.

    The watch scan may use a same-request Tencent depth quote, an all-A
    Tencent snapshot, or a Sina fallback.  They must never be stored under the
    same provider label: a later return calculation or freshness review needs
    to know exactly which source produced the price.
    """
    source = str((quote or {}).get("price_source") or "")
    if source == "sina_batched_watch_quote":
        return "sina_free"
    if source in {"tencent_batched_watch_quote", "tencent_all_a_snapshot"}:
        return "tencent_free"
    return "unknown_realtime_source"


def intraday_quote_exchange_time_status(quote: dict[str, Any] | None, observed_at: datetime,
                                        max_age_seconds: float) -> dict[str, Any]:
    """Classify an upstream quote timestamp against one Shanghai-clock SLO.

    Tencent emits one compact ``YYYYmmddHHMMSS`` field in its watch-depth
    adapter; Sina emits date/time separately.  Parsing is deliberately strict:
    a missing or malformed source timestamp cannot masquerade as a freshly
    fetched quote for an alert confirmation.
    """
    payload = quote or {}
    compact = "".join(re.findall(r"\d", str(payload.get("price_trade_time") or "")))
    date_part = "".join(re.findall(r"\d", str(payload.get("price_trade_date") or "")))
    if len(compact) >= 14:
        candidate = compact[:14]
    elif len(date_part) == 8 and len(compact) >= 6:
        candidate = f"{date_part}{compact[:6]}"
    else:
        return {"status": "missing_timestamp", "max_age_seconds": max_age_seconds}
    try:
        exchange_at = datetime.strptime(candidate, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return {"status": "invalid_timestamp", "max_age_seconds": max_age_seconds}
    age_seconds = (observed_at - exchange_at.astimezone(timezone.utc)).total_seconds()
    result = {"observed_trade_time": exchange_at.isoformat(), "age_seconds": round(age_seconds, 3),
              "max_age_seconds": max_age_seconds}
    if age_seconds < -5:
        return {**result, "status": "future_timestamp"}
    if age_seconds > max_age_seconds:
        return {**result, "status": "stale_timestamp"}
    return {**result, "status": "fresh"}


def intraday_quote_from_tencent(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize the limited Tencent fields used for a time-sensitive alert.

    This keeps the raw public row alongside normalized values.  ``zljlr`` is
    provider-labelled indicative main-flow data, not exchange order flow.
    """
    code = str(row.get("code") or "").strip()
    symbol = eastmoney_member_symbol({"代码": code[-6:]})
    if not symbol:
        return None
    return {
        "symbol": symbol, "name": row.get("name"), "price": intraday_number(row.get("zxj")),
        "pct_change": intraday_number(row.get("zdf")), "volume_ratio": intraday_number(row.get("lb")),
        "turnover_rate": intraday_number(row.get("hsl")), "main_net_inflow": intraday_number(row.get("zljlr")),
        "turnover": intraday_number(row.get("turnover")), "raw": dict(row),
        # This is a shared cross-sectional snapshot, not the dedicated batch
        # quote that confirms an alert for one explicit watchlist symbol.
        "price_source": "tencent_all_a_snapshot", "price_observed_from_depth": False,
    }


def annotate_intraday_flow_percentiles(quotes: dict[str, dict[str, Any]]) -> None:
    """Attach a cross-sectional main-flow percentile without assuming units.

    Tencent's public flow unit is provider-specific, so extreme buy/sell is
    judged against the same all-A snapshot instead of a fragile absolute yuan
    threshold.  This is the cross-sectional normalization pattern used by
    factor research systems, applied only to the observed universe.
    """
    ranked = sorted((quote for quote in quotes.values() if quote.get("main_net_inflow") is not None),
                    key=lambda quote: float(quote["main_net_inflow"]))
    denominator = max(1, len(ranked) - 1)
    for index, quote in enumerate(ranked):
        quote["main_flow_percentile"] = round(index / denominator, 5)
        quote["main_flow_rank"] = index + 1
        quote["main_flow_universe"] = len(ranked)


def intraday_minute_features(rows: list[dict[str, Any]], *, lookback: int = 20,
                             source: str = "tencent_free") -> dict[str, Any] | None:
    """Build a causal price/volume burst feature from normalized minute rows."""
    return pure_intraday_minute_features(rows, lookback=lookback, source=source, number=intraday_number)


def intraday_peer_context(peer_symbols: list[str], features: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Measure same-minute breadth without allowing the target into its peers."""
    return pure_intraday_peer_context(peer_symbols, features)


def post_close_exact_board_context(as_of_date: date) -> dict[str, dict[str, Any]]:
    """Join only exact THS member codes to same-date concept-flow evidence."""
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT member.symbol,flow.sector_key,sector.label,flow.net_amount,flow.change_pct,flow.leading_label,
                      flow.provider_key,flow.available_at
                 FROM quant.sector_membership_history member
                 JOIN quant.sector_market_observations flow
                   ON flow.taxonomy_key=member.taxonomy_key AND flow.sector_key=member.sector_key
                 JOIN quant.sectors sector ON sector.taxonomy_key=flow.taxonomy_key AND sector.sector_key=flow.sector_key
                WHERE member.taxonomy_key='ths_concept_flow' AND member.effective_to IS NULL
                  AND flow.taxonomy_key='ths_concept_flow' AND flow.trading_date=%s""",
            (as_of_date,),
        ).fetchall()
    return pure_exact_board_context([dict(row) for row in rows], json_safe=strategy_json_safe)


def post_close_tushare_lhb_context(as_of_date: date) -> dict[str, dict[str, Any]]:
    """Aggregate deduplicated post-close institution-seat evidence by symbol."""
    stamp = as_of_date.strftime("%Y%m%d")
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT api_name,row_data,provider_key,available_at
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('top_list','top_inst') AND row_data->>'trade_date'=%s
                ORDER BY available_at DESC""", (stamp,),
        ).fetchall()
    return pure_lhb_context([dict(row) for row in rows], number=intraday_number)


def post_close_strategy_candidates(as_of_date: date, limit: int, minimum_full_market_symbols: int) -> dict[str, Any]:
    """Compatibility entry point for the isolated persisted-only service."""
    return persisted_post_close_strategy_candidates(
        db, as_of_date, limit, minimum_full_market_symbols,
        board_context=post_close_exact_board_context, screen=pure_post_close_screen_candidates,
        daily_base_structure=daily_base_structure, forming_structure=post_close_forming_structure,
        fresh_start_structure=post_close_fresh_start_structure,
    )


def run_post_close_strategy(request: PostCloseStrategyRequest) -> dict[str, Any]:
    """Compatibility entry point for the isolated persisted-only service."""
    return persisted_run_post_close_strategy(
        db, request, model_version=POST_CLOSE_STRATEGY_MODEL_VERSION,
        candidate_loader=post_close_strategy_candidates, json_safe=strategy_json_safe,
    )


STRATEGY_PATTERN_MODEL_VERSION = "post-close-limit-lift-pattern-v6"
TENCENT_INTRADAY_MINUTE_CAPABILITY = "intraday_minute"
LOCAL_CAPACITY_HTTP_DETAIL = "local processing capacity is temporarily saturated; retry shortly"


def is_local_capacity_http_error(error: HTTPException) -> bool:
    """Recognize only the service's explicit local-backpressure response."""
    return error.status_code == 503 and str(error.detail) == LOCAL_CAPACITY_HTTP_DETAIL


def is_circuit_open_http_error(error: HTTPException) -> bool:
    """Keep a provider protection decision distinct from a failed call."""
    return error.status_code == 503 and "circuit-open" in str(error.detail)


def persist_tencent_intraday_minute_health(completed: int, errors: list[str], latency_ms: int | None = None) -> None:
    """Persist one aggregate minute-tape outcome, never one health row per symbol."""
    with db.transaction() as connection:
        if completed:
            record_provider_success(connection, "tencent_free", TENCENT_INTRADAY_MINUTE_CAPABILITY, completed, latency_ms)
        elif errors:
            record_provider_failure(connection, "tencent_free", TENCENT_INTRADAY_MINUTE_CAPABILITY,
                                    " | ".join(errors)[:500], latency_ms)


def limit_board_count(tag: Any) -> int:
    """Extract the number of successful boards without overstating continuity."""
    return pure_limit_board_count(tag)


def post_close_limit_daily_features(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the selected limit-up session against only earlier daily bars."""
    return pure_limit_daily_features(bars, number=intraday_number, limit_ratio=a_share_limit_ratio)

def _strategy_session_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep continuous-auction minutes and one value per minute."""
    return pure_strategy_session_rows(rows, number=intraday_number)


def intraday_limit_lift_pattern(rows: list[dict[str, Any]], daily: dict[str, Any]) -> dict[str, Any]:
    """Compatibility export backed by the isolated causal pattern module."""
    return pure_intraday_limit_lift_pattern(
        rows, daily, number=intraday_number, limit_ratio=a_share_limit_ratio,
        minute_features=intraday_minute_features,
        session_rows=lambda session_rows, number: _strategy_session_rows(session_rows),
    )


async def refresh_strategy_pattern_sources(as_of_date: date) -> dict[str, Any]:
    """Refresh the small same-day limit ladder before selecting replay samples."""
    stamp = as_of_date.strftime("%Y%m%d")
    results: dict[str, Any] = {}
    for api_name in ("limit_list_ths", "limit_step", "limit_cpt_list", "top_list", "top_inst"):
        try:
            outcome = await fetch_tushare_catalog(TushareFetchRequest(
                api_name=api_name, provider="auto", params={"trade_date": stamp}, max_rows=3000, force_refresh=True,
            ))
            results[api_name] = {key: outcome.get(key) for key in ("status", "provider", "received", "stored", "request_key")}
        except HTTPException as error:
            results[api_name] = {"status": "failed", "error": str(error.detail)[:300]}
    return results


def merge_limit_pool_sources(ths_rows: list[dict[str, Any]], eastmoney_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the complete locally observable limit-up union without truncating to replay samples."""
    merged: dict[str, dict[str, Any]] = {}
    ths_symbols: set[str] = set()
    eastmoney_symbols: set[str] = set()
    for stored in ths_rows:
        raw = dict(stored.get("row_data") or stored)
        symbol = str(raw.get("ts_code") or "").upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            continue
        ths_symbols.add(symbol)
        merged[symbol] = {
            **strategy_json_safe(raw), "ts_code": symbol,
            "provider_key": stored.get("provider_key") or "tushare_super_sdk",
            "available_at": stored.get("available_at"), "sources": ["tushare_limit_list_ths"],
        }
    for stored in eastmoney_rows:
        symbol = str(stored.get("symbol") or "").upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            continue
        eastmoney_symbols.add(symbol)
        body = stored.get("body") or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = {}
        raw = dict(body) if isinstance(body, dict) else {}
        board_count = int(intraday_number(raw.get("连板数")) or 1)
        eastmoney = {
            "ts_code": symbol, "name": raw.get("名称"), "limit_type": "涨停池",
            "pct_chg": intraday_number(raw.get("涨跌幅")), "price": intraday_number(raw.get("最新价")),
            "amount": intraday_number(raw.get("成交额")), "turnover_rate": intraday_number(raw.get("换手率")),
            "limit_amount": intraday_number(raw.get("封板资金")), "first_time": raw.get("首次封板时间"),
            "last_time": raw.get("最后封板时间"), "open_num": intraday_number(raw.get("炸板次数")),
            "tag": "首板" if board_count <= 1 else f"{board_count}连板",
            "lu_desc": raw.get("所属行业"), "provider_key": stored.get("source") or "akshare",
            "available_at": stored.get("available_at"), "sources": ["eastmoney_stock_zt_pool_em"],
        }
        if symbol in merged:
            for key, value in eastmoney.items():
                if key not in {"provider_key", "available_at", "sources"} and merged[symbol].get(key) in {None, ""} and value not in {None, ""}:
                    merged[symbol][key] = value
            merged[symbol]["sources"] = [*merged[symbol].get("sources", []), "eastmoney_stock_zt_pool_em"]
            merged[symbol]["eastmoney_evidence"] = strategy_json_safe(eastmoney)
        else:
            merged[symbol] = eastmoney
    union_symbols = ths_symbols | eastmoney_symbols
    return {
        "items": list(merged.values()),
        "coverage": {
            "status": "two_source_union" if ths_symbols and eastmoney_symbols else "single_source_only",
            "union_count": len(union_symbols), "intersection_count": len(ths_symbols & eastmoney_symbols),
            "tushare_count": len(ths_symbols), "eastmoney_count": len(eastmoney_symbols),
            "tushare_only": sorted(ths_symbols - eastmoney_symbols),
            "eastmoney_only": sorted(eastmoney_symbols - ths_symbols), "local_truncation": False,
            "notice": "完整表示当前已访问同花顺与东财涨停池的去重并集，不代表交易所官方全量保证。",
        },
    }


def strategy_pattern_sample_candidates(as_of_date: date, max_symbols: int, per_cohort: int,
                                       focus_symbols: list[str] | None = None) -> dict[str, Any]:
    """Read persisted sample inputs, then delegate deterministic ranking."""
    stamp = as_of_date.strftime("%Y%m%d")
    with db.transaction() as connection:
        limit_rows = connection.execute(
            """SELECT DISTINCT ON(row_data->>'ts_code') row_data,provider_key,available_at
                 FROM quant.tushare_raw_records WHERE api_name='limit_list_ths'
                  AND row_data->>'trade_date'=%s AND row_data->>'limit_type'='涨停池'
                ORDER BY row_data->>'ts_code',available_at DESC""", (stamp,),
        ).fetchall()
        step_rows = connection.execute(
            """SELECT DISTINCT ON(row_data->>'ts_code') row_data,available_at
                 FROM quant.tushare_raw_records WHERE api_name='limit_step' AND row_data->>'trade_date'=%s
                ORDER BY row_data->>'ts_code',available_at DESC""", (stamp,),
        ).fetchall()
        prior_date_row = connection.execute(
            """SELECT max(row_data->>'trade_date') prior_date FROM quant.tushare_raw_records
                WHERE api_name='limit_list_ths' AND row_data->>'trade_date'<%s""", (stamp,),
        ).fetchone()
        prior_stamp = prior_date_row["prior_date"] if prior_date_row else None
        prior_limit_rows = connection.execute(
            """SELECT DISTINCT ON(row_data->>'ts_code') row_data
                 FROM quant.tushare_raw_records WHERE api_name='limit_list_ths'
                  AND row_data->>'trade_date'=%s AND row_data->>'limit_type'='涨停池'
                ORDER BY row_data->>'ts_code',available_at DESC""", (prior_stamp,),
        ).fetchall() if prior_stamp else []
        symbols = [str(row["row_data"].get("ts_code") or "").upper() for row in limit_rows]
        daily_rows = connection.execute(
            """WITH ranked AS (
                   SELECT b.*,row_number() OVER(PARTITION BY b.symbol ORDER BY b.trading_date DESC) rn
                     FROM quant.canonical_bars_daily b WHERE b.symbol=ANY(%s)
                      AND b.trading_date<=%s AND b.trading_date>=%s
                 ) SELECT * FROM ranked WHERE rn<=21 ORDER BY symbol,trading_date""",
            (symbols, as_of_date, as_of_date - timedelta(days=60)),
        ).fetchall() if symbols else []
    return pure_post_close_pattern_candidates(
        as_of_date, max_symbols, per_cohort, [dict(row) for row in limit_rows],
        [dict(row["row_data"] or {}) for row in step_rows], [dict(row["row_data"] or {}) for row in prior_limit_rows],
        [dict(row) for row in daily_rows], post_close_exact_board_context(as_of_date),
        post_close_tushare_lhb_context(as_of_date), focus_symbols,
        limit_daily_features=post_close_limit_daily_features, board_count=limit_board_count,
    )


def strategy_pattern_review_score(item: dict[str, Any], pattern: dict[str, Any], risk_flags: list[str]) -> dict[str, Any]:
    """Compatibility export backed by pure post-close scoring."""
    return pure_pattern_review_score(item, pattern, risk_flags, number=intraday_number)


def latest_strategy_pattern_date() -> date | None:
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT max(trading_date) latest FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH'"
        ).fetchone()
    return row["latest"] if row else None


def persist_strategy_pattern_run(
    run_key: str,
    as_of_date: date,
    status: str,
    source_status: dict[str, Any],
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
) -> Any:
    """Replace one bounded pattern-mining run atomically in a DB worker."""
    with db.transaction() as connection:
        run = connection.execute(
            """INSERT INTO quant.strategy_pattern_runs(run_key,as_of_date,model_version,status,source_status,summary)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(run_key) DO UPDATE SET status=EXCLUDED.status,source_status=EXCLUDED.source_status,
                 summary=EXCLUDED.summary,updated_at=now() RETURNING run_id""",
            (run_key, as_of_date, STRATEGY_PATTERN_MODEL_VERSION, status,
             Json(strategy_json_safe(source_status)), Json(strategy_json_safe(summary))),
        ).fetchone()
        connection.execute("DELETE FROM quant.strategy_pattern_samples WHERE run_id=%s", (run["run_id"],))
        for rank, sample in enumerate(samples, start=1):
            connection.execute(
                """INSERT INTO quant.strategy_pattern_samples(run_id,rank,symbol,name,primary_cohort,cohorts,board_context,
                       limit_context,daily_features,intraday_pattern,minute_source,risk_flags)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run["run_id"], rank, sample["symbol"], sample.get("name"), sample["primary_cohort"], Json(sample["cohorts"]),
                 Json(strategy_json_safe(sample["board_context"])), Json(strategy_json_safe(sample["limit_context"])),
                 Json(strategy_json_safe(sample["daily_features"])), Json(strategy_json_safe(sample["intraday_pattern"])),
                 sample.get("minute_source"), Json(sample["risk_flags"])),
            )
    return run["run_id"]


async def run_strategy_pattern_mining(request: StrategyPatternMiningRequest) -> dict[str, Any]:
    """Fetch bounded Tencent minute tapes and persist compact replay evidence."""
    latest = await run_database_blocking(latest_strategy_pattern_date)
    as_of_date = request.as_of_date or latest
    if as_of_date is None:
        return {"status": "blocked", "reason": "no daily bars are stored", "samples": []}
    limit_sources = await refresh_strategy_pattern_sources(as_of_date) if request.refresh_limit_sources else {"status": "skipped"}
    selection = await run_database_blocking(
        strategy_pattern_sample_candidates, as_of_date, request.max_symbols, request.per_cohort, request.focus_symbols,
    )
    candidates = selection.get("candidates", [])
    minute_circuit_open = bool(candidates) and TENCENT_INTRADAY_MINUTE_CAPABILITY in await open_provider_capabilities(
        "tencent_free", [TENCENT_INTRADAY_MINUTE_CAPABILITY],
    )
    semaphore = asyncio.Semaphore(4)

    async def replay(item: dict[str, Any]) -> dict[str, Any]:
        try:
            async with semaphore:
                rows = await asyncio.wait_for(tencent_intraday_minutes(item["symbol"]), timeout=10)
            pattern = intraday_limit_lift_pattern(rows, item["daily_features"])
            risk_flags = list(item["risk_flags"])
            if item["daily_features"].get("ground_to_sky_daily_shape") and "ground_to_sky_reversal" not in pattern.get("pattern_tags", []):
                risk_flags.append("daily_minute_extreme_path_mismatch")
            review = strategy_pattern_review_score(item, pattern, risk_flags)
            return {**item, "limit_context": {**item["limit_context"], **review},
                    "intraday_pattern": pattern, "minute_source": "tencent_free_minute", "risk_flags": risk_flags}
        except (asyncio.TimeoutError, httpx.HTTPError, FreeProviderError, ValueError) as error:
            return {**item, "intraday_pattern": {"status": "failed", "error": str(error)[:240], "curve": []},
                    "minute_source": "tencent_free_minute", "risk_flags": [*item["risk_flags"], "minute_replay_failed"]}

    if minute_circuit_open:
        samples = [{**item, "intraday_pattern": {"status": "blocked", "error": "provider health circuit is open; upstream request skipped", "curve": []},
                    "minute_source": "tencent_free_minute", "risk_flags": [*item["risk_flags"], "minute_replay_circuit_open"]}
                   for item in candidates]
    else:
        started_at = asyncio.get_running_loop().time()
        samples = await asyncio.gather(*(replay(item) for item in candidates))
        if candidates:
            completed_count = sum(item["intraday_pattern"].get("status") == "completed" for item in samples)
            errors = [str(item["intraday_pattern"].get("error") or "minute replay failed") for item in samples
                      if item["intraday_pattern"].get("status") != "completed"]
            await run_database_blocking(
                persist_tencent_intraday_minute_health, completed_count, errors,
                round((asyncio.get_running_loop().time() - started_at) * 1000),
            )
    samples.sort(key=lambda item: (-float(item.get("limit_context", {}).get("review_score") or 0), item["symbol"]))
    failed = [item for item in samples if item["intraday_pattern"].get("status") != "completed"]
    status = "blocked" if minute_circuit_open or not samples else "partial" if failed else "completed"
    pattern_counts: dict[str, int] = {}
    for item in samples:
        for tag in item["intraday_pattern"].get("pattern_tags", []):
            pattern_counts[str(tag)] = pattern_counts.get(str(tag), 0) + 1
    picks = [item for item in samples if item.get("limit_context", {}).get("review_tier") != "research_sample"][:10]
    summary = {"selected": len(samples), "picks": len(picks), "minute_completed": len(samples) - len(failed), "minute_failed": len(failed),
               "cohort_counts": selection.get("cohort_counts", {}), "pattern_counts": pattern_counts,
               "limit_pool_rows": selection.get("limit_pool_rows", 0), "limit_step_rows": selection.get("limit_step_rows", 0),
               "dragon_leader_market_context": selection.get("dragon_leader_market_context", {})}
    source_status = {"daily": "canonical_bars_daily", "limit_sources": limit_sources,
                     "minute": {"provider": "tencent_free", "status": "circuit_open" if minute_circuit_open else status,
                                "completed": len(samples) - len(failed),
                                "failed": {item["symbol"]: item["intraday_pattern"].get("error") for item in failed}},
                     "super_get_minute": "corroborating source when healthy; Tencent is the bounded post-close replay source"}
    run_key = hashlib.sha256(f"{STRATEGY_PATTERN_MODEL_VERSION}:{as_of_date}".encode()).hexdigest()
    run_id = await run_database_blocking(
        persist_strategy_pattern_run, run_key, as_of_date, status, source_status, summary, samples, timeout_seconds=60,
    )
    return {"status": status, "as_of_date": str(as_of_date), "run_id": str(run_id),
            "model_version": STRATEGY_PATTERN_MODEL_VERSION, "summary": summary, "source_status": source_status,
            "picks": [{**item, "rank": rank} for rank, item in enumerate(picks, start=1)],
            "samples": [{**item, "rank": rank} for rank, item in enumerate(samples, start=1)],
            "notice": "样本用于发现可证伪的盘中形态；地天板只产生研究观察和承接检查，不自动下单。"}


def watchlist_daily_factors(symbol: str, connection: Any | None = None) -> dict[str, Any]:
    """Compute a small, explainable Alpha158-inspired daily factor subset."""
    # The intraday persistence path already owns one transaction.  Accepting
    # that connection avoids opening a nested connection once per watched
    # symbol, while the optional standalone path remains convenient for
    # on-registration factor preparation.
    if connection is None:
        with db.transaction() as owned_connection:
            return watchlist_daily_factors(symbol, owned_connection)
    return pure_watchlist_daily_factors(symbol, connection, number=intraday_number)


intraday_feature_clock = pure_intraday_feature_clock
intraday_eac_window = pure_intraday_eac_window
intraday_minute_bucket = pure_intraday_minute_bucket


def intraday_volume_time_profile(symbol: str, minute_time: Any, as_of_date: date,
                                 connection: Any | None = None) -> dict[str, Any]:
    """Build a strictly prior-day, same-minute volume baseline for one symbol."""
    if connection is None:
        with db.transaction() as owned_connection:
            return intraday_volume_time_profile(symbol, minute_time, as_of_date, owned_connection)
    return pure_intraday_volume_time_profile(
        symbol, minute_time, as_of_date, connection,
        minute_bucket_fn=intraday_minute_bucket, number=intraday_number,
    )


def attach_intraday_volume_time_profile(symbol: str, minute_feature: dict[str, Any] | None,
                                        observed_at: datetime, connection: Any | None = None) -> dict[str, Any] | None:
    """Attach the point-in-time volume surprise without leaking today's close."""
    if minute_feature is None:
        return None
    profile = intraday_volume_time_profile(
        symbol, minute_feature.get("time"), observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date(), connection,
    )
    return pure_attach_volume_time_profile(minute_feature, profile, number=intraday_number)


def intraday_upside_research_assessment(quote: dict[str, Any] | None, daily_factors: dict[str, Any] | None,
                                        minute_features: dict[str, Any] | None,
                                        peer_context: dict[str, Any] | None) -> dict[str, Any]:
    """Compatibility export backed by the pure breakout assessor."""
    return pure_upside_research_assessment(
        quote, daily_factors, minute_features, peer_context,
        number=intraday_number, eac_window=intraday_eac_window,
    )


def intraday_eac_acceptance_assessment(first_conditions: dict[str, Any] | None, *,
                                        first_observed_at: datetime, observed_at: datetime,
                                        quote: dict[str, Any] | None, previous_quote: dict[str, Any] | None,
                                        minute_features: dict[str, Any] | None,
                                        peer_context: dict[str, Any] | None) -> dict[str, Any]:
    """Compatibility export backed by the pure acceptance assessor."""
    return pure_eac_acceptance_assessment(
        first_conditions, first_observed_at=first_observed_at, observed_at=observed_at,
        quote=quote, previous_quote=previous_quote, minute_features=minute_features,
        peer_context=peer_context, number=intraday_number,
        confirmation_window_seconds=INTRADAY_CONFIRMATION_WINDOW.total_seconds(),
    )


WATCHLIST_FACTOR_MODEL_VERSION = "qlib-lean-watchlist-v1"


async def hydrate_watchlist_history(watchlist_id: uuid.UUID, symbol: str) -> dict[str, Any]:
    """Fetch bounded history on pool registration and persist factor evidence."""
    end_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start_date = end_date - timedelta(days=45)
    dated = {"ts_code": symbol, "start_date": start_date.strftime("%Y%m%d"), "end_date": end_date.strftime("%Y%m%d")}
    daily_result = await sync_tushare(TushareSyncRequest(symbols=[symbol], start_date=start_date, end_date=end_date))
    supplemental = await asyncio.gather(
        stock_study_fetch("watchlist_adj_factor", TushareFetchRequest(api_name="adj_factor", params=dated, max_rows=60)),
        stock_study_fetch("watchlist_daily_basic", TushareFetchRequest(api_name="daily_basic", params=dated, max_rows=60)),
        stock_study_fetch("watchlist_moneyflow", TushareFetchRequest(api_name="moneyflow", params=dated, max_rows=60)),
        stock_study_fetch("watchlist_moneyflow_dc", TushareFetchRequest(api_name="moneyflow_dc", params=dated, max_rows=60)),
    )
    source_status = {"daily": daily_result, **{item[0]["source"]: item[0] for item in supplemental}}
    factors = await run_database_blocking(watchlist_daily_factors, symbol)
    daily_ok = daily_result.get("status") in {"completed", "partial", "unchanged"} and int(factors.get("bar_count") or 0) >= 21
    supplemental_ok = sum(1 for item, _ in supplemental if item.get("status") in {"completed", "partial", "unchanged"})
    status = "completed" if daily_ok and supplemental_ok >= 2 else "partial" if daily_ok else "failed"
    factors.update({"factor_family": ["qlib_price_volume_rolling", "rsi14", "ma_trend", "lean_separate_risk_layer"],
                    "factor_ready": daily_ok, "supplemental_sources_ready": supplemental_ok})

    def persist_factor_snapshot() -> None:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.watchlist_factor_snapshots(watchlist_id,symbol,observed_at,lookback_calendar_days,status,source_status,factors,model_version)
                   VALUES(%s,%s,now(),45,%s,%s,%s,%s)""",
                (watchlist_id, symbol, status, Json(strategy_json_safe(source_status)), Json(strategy_json_safe(factors)), WATCHLIST_FACTOR_MODEL_VERSION),
            )

    await run_database_blocking(persist_factor_snapshot)
    return {"status": status, "start_date": str(start_date), "end_date": str(end_date), "source_status": source_status,
            "factors": factors, "notice": "因子用于盘中提醒分层与后续回测，不构成自动交易指令。"}


def intraday_signal_rules(watch: dict[str, Any], quote: dict[str, Any] | None,
                           previous_quote: dict[str, Any] | None, daily_factors: dict[str, Any] | None = None,
                           minute_features: dict[str, Any] | None = None,
                           peer_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Compatibility export backed by the pure live/replay signal rules."""
    observed_at = (quote or {}).get("_scan_observed_at") if isinstance(quote, dict) else None
    opening_gap_window = (
        isinstance(observed_at, datetime)
        and time(9, 30) <= observed_at.astimezone(ZoneInfo("Asia/Shanghai")).time() < time(9, 40)
    )
    return pure_intraday_signal_rules(
        watch, quote, previous_quote, daily_factors, minute_features, peer_context,
        number=intraday_number, upside_assessment_fn=intraday_upside_research_assessment,
        model_version=INTRADAY_SIGNAL_MODEL_VERSION, opening_gap_window=opening_gap_window,
    )


def decision_card_url(symbol: str) -> str | None:
    """Return a human-reachable review link only when the operator configured one."""
    base_url = (os.getenv("QUANT_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/?section=research&tab=stock-study&symbol={symbol}"


async def attempt_intraday_alert_delivery(delivery_id: uuid.UUID, signal_event_id: uuid.UUID, text: str) -> dict[str, Any]:
    """Send one durable outbox item and keep failed content available for retry."""
    outcome = await post_feishu_alert_text(text)

    def persist_delivery_attempt() -> dict[str, Any] | None:
        with db.transaction() as connection:
            # Ignore the current pending row while calculating the preceding
            # streak.  Include every existing Feishu delivery family so the
            # dashboard and recovery receipt have one consistent meaning.
            history_rows = connection.execute(
                """SELECT status FROM (
                       SELECT status,created_at FROM quant.intraday_alert_deliveries
                        WHERE delivery_id<>%s
                       UNION ALL
                       SELECT status,created_at FROM quant.intraday_board_report_deliveries
                       UNION ALL
                       SELECT delivery_status AS status,updated_at AS created_at FROM quant.strategy_day_summaries
                   ) delivery ORDER BY created_at DESC LIMIT 20""",
                (delivery_id,),
            ).fetchall()
            prior_failed_streak = 0
            for row in history_rows:
                status = str(row["status"])
                if status == "pending":
                    continue
                if status == "failed":
                    prior_failed_streak += 1
                    continue
                break
            connection.execute(
                """UPDATE quant.intraday_alert_deliveries
                      SET status=%s,response=%s,error_message=%s,
                          sent_at=%s,attempt_count=attempt_count+1,
                          next_attempt_at=CASE WHEN %s='failed' AND attempt_count+1<%s
                                               THEN now()+interval '30 seconds' ELSE NULL END
                    WHERE delivery_id=%s""",
                (outcome["status"], Json(strategy_json_safe(outcome.get("response", {}))), outcome.get("error") or outcome.get("reason"),
                 datetime.now(timezone.utc) if outcome["status"] == "sent" else None,
                 outcome["status"], INTRADAY_ALERT_MAX_ATTEMPTS, delivery_id),
            )
            if outcome["status"] == "sent":
                connection.execute("UPDATE quant.intraday_signal_events SET state='alerted' WHERE signal_event_id=%s", (signal_event_id,))
            if outcome["status"] == "failed" and prior_failed_streak + 1 == 3:
                event = connection.execute(
                    """INSERT INTO quant.alert_delivery_health_events(
                           channel,event_type,source_reference,streak_count,delivery_status,message_text
                       ) VALUES('feishu_adapter','failure_streak',%s,%s,'observed',%s)
                       ON CONFLICT(channel,event_type,source_reference) DO NOTHING
                       RETURNING health_event_id,event_type,streak_count,message_text""",
                    (str(delivery_id), prior_failed_streak + 1,
                     f"Feishu alert delivery has failed {prior_failed_streak + 1} consecutive times"),
                ).fetchone()
                return dict(event) if event else None
            if outcome["status"] == "sent" and prior_failed_streak >= 3:
                message = delivery_health_recovery_text(prior_failed_streak)
                event = connection.execute(
                    """INSERT INTO quant.alert_delivery_health_events(
                           channel,event_type,source_reference,streak_count,delivery_status,message_text
                       ) VALUES('feishu_adapter','recovered',%s,%s,'pending',%s)
                       ON CONFLICT(channel,event_type,source_reference) DO NOTHING
                       RETURNING health_event_id,event_type,streak_count,message_text""",
                    (str(delivery_id), prior_failed_streak, message),
                ).fetchone()
                return dict(event) if event else None
        return None

    health_event = await run_database_blocking(persist_delivery_attempt)
    if health_event and health_event["event_type"] == "recovered":
        health_outcome = await post_feishu_alert_text(str(health_event["message_text"]))

        def persist_health_event_attempt() -> None:
            with db.transaction() as connection:
                connection.execute(
                    """UPDATE quant.alert_delivery_health_events
                          SET delivery_status=%s,response=%s,error_message=%s,sent_at=%s,updated_at=now()
                        WHERE health_event_id=%s""",
                    (health_outcome["status"], Json(strategy_json_safe(health_outcome.get("response", {}))),
                     health_outcome.get("error") or health_outcome.get("reason"),
                     datetime.now(timezone.utc) if health_outcome["status"] == "sent" else None,
                     health_event["health_event_id"]),
                )
        await run_database_blocking(persist_health_event_attempt)
    return outcome


async def deliver_intraday_alert(signal_event_id: uuid.UUID, text: str) -> dict[str, Any]:
    """Persist before outbound I/O so a short-lived signal cannot be lost."""
    def create_pending_delivery() -> uuid.UUID:
        with db.transaction() as connection:
            row = connection.execute(
                """INSERT INTO quant.intraday_alert_deliveries(
                       signal_event_id,channel,status,message_text,next_attempt_at
                   ) VALUES(%s,'feishu_adapter','pending',%s,now()) RETURNING delivery_id""",
                (signal_event_id, text),
            ).fetchone()
        return row["delivery_id"]

    delivery_id = await run_database_blocking(create_pending_delivery)
    return await attempt_intraday_alert_delivery(delivery_id, signal_event_id, text)


async def retry_pending_intraday_alerts(limit: int = 3) -> dict[str, int]:
    """Retry bounded, unsent outbox rows even when their source signal faded."""
    def load_due() -> list[dict[str, Any]]:
        with db.transaction() as connection:
            rows = connection.execute(
                """SELECT d.delivery_id,d.signal_event_id,d.message_text
                     FROM quant.intraday_alert_deliveries d
                    WHERE d.channel='feishu_adapter' AND d.status IN ('pending','failed')
                      AND d.message_text IS NOT NULL AND d.message_text<>''
                      AND d.attempt_count<%s
                      AND coalesce(d.next_attempt_at,d.created_at)<=now()
                      AND NOT EXISTS (
                          SELECT 1 FROM quant.intraday_alert_deliveries sent
                           WHERE sent.signal_event_id=d.signal_event_id AND sent.status='sent'
                      )
                    ORDER BY d.created_at LIMIT %s""",
                (INTRADAY_ALERT_MAX_ATTEMPTS, max(1, min(limit, 10))),
            ).fetchall()
        return [dict(row) for row in rows]

    rows = await run_database_blocking(load_due)
    sent = failed = disabled = 0
    for row in rows:
        outcome = await attempt_intraday_alert_delivery(row["delivery_id"], row["signal_event_id"], str(row["message_text"]))
        if outcome["status"] == "sent":
            sent += 1
        elif outcome["status"] == "failed":
            failed += 1
        else:
            disabled += 1
    return {"loaded": len(rows), "sent": sent, "failed": failed, "disabled": disabled}


async def intraday_tushare_minutes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Get a bounded minute feature window through the verified super GET path."""
    async def fetch_rows(symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        source, rows = await stock_study_fetch("tushare_rt_min", TushareFetchRequest(
            api_name="rt_min", provider="super", params={"ts_code": symbol, "freq": "1MIN"}, max_rows=30, force_refresh=True,
        ))
        return source, rows

    return await fetch_bounded_minute_context(
        symbols, fetch_rows=fetch_rows, feature_builder=intraday_minute_features, number=intraday_number,
    )


def intraday_minute_profile_capture_enabled() -> bool:
    return os.getenv("INTRADAY_MINUTE_PROFILE_CAPTURE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def intraday_minute_profile_retention_days() -> int:
    try:
        return max(20, min(365, int(os.getenv("INTRADAY_MINUTE_PROFILE_RETENTION_DAYS", "90"))))
    except ValueError:
        return 90


def intraday_minute_profile_max_symbols() -> int:
    """Bound the close capture without silently reducing the normal pool."""
    try:
        return max(1, min(40, int(os.getenv("INTRADAY_MINUTE_PROFILE_MAX_SYMBOLS", "40"))))
    except ValueError:
        return 40


def intraday_watch_priority_key(row: dict[str, Any]) -> tuple[int, int, str]:
    """Keep the small verified-minute budget on explicitly enabled research watches."""
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    research_enabled = any(isinstance(metadata.get(key), dict) and metadata[key].get("enabled")
                           for key in ("surge_strategy", "reversal_research", "upside_research"))
    return (0 if research_enabled else 1, -int(row.get("available_quantity") or 0), str(row["symbol"]))


def intraday_order_book_enabled() -> bool:
    return os.getenv("INTRADAY_ORDER_BOOK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def intraday_order_book_interval_seconds() -> float:
    try:
        return max(3.0, min(30.0, float(os.getenv("INTRADAY_ORDER_BOOK_INTERVAL_SECONDS", "3"))))
    except ValueError:
        return 3.0


def intraday_order_book_retention_days() -> int:
    """Keep high-frequency depth evidence bounded independently of rt_k."""
    try:
        return max(1, min(30, int(os.getenv("INTRADAY_ORDER_BOOK_RETENTION_DAYS", "7"))))
    except ValueError:
        return 7


def intraday_order_book_max_symbols() -> int:
    """Bound a single Tencent depth batch without silently losing watches."""
    try:
        return max(1, min(80, int(os.getenv("INTRADAY_ORDER_BOOK_MAX_SYMBOLS", "40"))))
    except ValueError:
        return 40


def persist_intraday_order_book_observations(observed_at: datetime, rows: list[dict[str, Any]], latency_ms: int) -> int:
    """Persist raw order-book evidence plus derived observational features."""
    stored = 0
    previous_cutoff = observed_at - timedelta(seconds=15)
    china_observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    session_start = china_observed_at.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    symbols = sorted({str(row.get("ts_code") or "") for row in rows if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("ts_code") or ""))})
    with db.transaction() as connection:
        # One source-qualified lookup for the entire batch.  The partial index
        # prevents rt_k rows from expanding this high-frequency query.
        previous_rows = connection.execute(
            """SELECT DISTINCT ON(symbol) symbol,observed_at,raw
                 FROM quant.intraday_quote_observations
                WHERE symbol=ANY(%s) AND source_name='tencent_order_book'
                  AND observed_at>=%s AND observed_at<%s
                ORDER BY symbol,observed_at DESC""",
            (symbols, session_start, observed_at),
        ).fetchall() if symbols else []
        previous_by_symbol = {str(item["symbol"]): dict(item) for item in previous_rows}
        for row in rows:
            symbol = str(row.get("ts_code") or "")
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                continue
            previous = previous_by_symbol.get(symbol)
            previous_is_fresh = bool(previous and previous["observed_at"] >= previous_cutoff)
            previous_raw = dict(previous["raw"] or {}) if previous_is_fresh else None
            features = order_book_observation(row, previous_raw)
            if previous and not previous_is_fresh:
                features["delta_status"] = "stale_previous"
            raw = {**row, "order_book_features": features}
            inserted = connection.execute(
                """INSERT INTO quant.intraday_quote_observations(
                       scan_id,symbol,observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow,raw
                   ) VALUES(NULL,%s,%s,'tencent_order_book',%s,%s,NULL,NULL,NULL,%s)
                   ON CONFLICT(symbol,source_name,observed_at) DO NOTHING""",
                (symbol, observed_at, row.get("price"),
                 ((float(row["price"]) / float(row["pre_close"])) - 1) * 100 if row.get("pre_close") else None,
                 Json(strategy_json_safe(raw))),
            )
            stored += int(inserted.rowcount > 0)
        record_provider_success(connection, "tencent_free", "order_book_quote", stored, latency_ms)
    return stored


def persist_intraday_order_book_failure(error: str, latency_ms: int | None = None) -> None:
    with db.transaction() as connection:
        record_provider_failure(connection, "tencent_free", "order_book_quote", error, latency_ms)


async def capture_intraday_order_book_snapshot(symbols: list[str]) -> dict[str, Any]:
    """Capture one pooled, bounded depth snapshot for the explicit watchlist."""
    max_symbols = intraday_order_book_max_symbols()
    selected = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(symbol).upper())))[:max_symbols]
    if not selected:
        return {"status": "completed", "requested": 0, "stored": 0}
    started_at = asyncio.get_running_loop().time()
    observed_at = datetime.now(timezone.utc)
    try:
        rows = await tencent_order_book_quotes(selected, max_symbols=max_symbols)
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        stored = await run_database_blocking(
            persist_intraday_order_book_observations, observed_at, rows, latency_ms,
        )
        return {"status": "completed" if rows else "empty", "requested": len(selected), "received": len(rows),
                "stored": stored, "observed_at": observed_at.isoformat(), "latency_ms": latency_ms,
                "source": "tencent_single_quote_order_book"}
    except (httpx.HTTPError, FreeProviderError, ValueError, ExecutorSaturatedError, asyncio.TimeoutError) as error:
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database_blocking(persist_intraday_order_book_failure, str(error)[:300], latency_ms)
        return {"status": "failed", "requested": len(selected), "stored": 0,
                "reason": safe_error_detail(str(error), 300)}


async def intraday_order_book_loop() -> None:
    """Observe watchlist depth at a bounded cadence; never derive an order."""
    pruned_on: date | None = None
    while True:
        active, _ = await realtime_market_session_async()
        if active:
            circuit_open = "order_book_quote" in await open_provider_capabilities("tencent_free", ["order_book_quote"])
            if circuit_open:
                await asyncio.sleep(max(15.0, intraday_order_book_interval_seconds()))
                continue
            def load_watches() -> list[Any]:
                with db.transaction() as connection:
                    return connection.execute(
                        "SELECT symbol FROM quant.intraday_watchlists WHERE enabled ORDER BY updated_at DESC,symbol LIMIT %s",
                        (intraday_order_book_max_symbols(),),
                    ).fetchall()
            rows = await run_database_blocking(load_watches)
            local_date = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
            if pruned_on != local_date:
                cutoff = datetime.now(timezone.utc) - timedelta(days=intraday_order_book_retention_days())
                def prune() -> None:
                    with db.transaction() as connection:
                        connection.execute(
                            "DELETE FROM quant.intraday_quote_observations "
                            "WHERE source_name='tencent_order_book' AND observed_at<%s",
                            (cutoff,),
                        )
                await run_database_blocking(prune)
                pruned_on = local_date
            allowed, storage = await nonessential_high_frequency_capture_allowed()
            if not allowed:
                print(f"intraday order-book capture skipped by storage guard: {storage.get('state')}")
                await asyncio.sleep(intraday_order_book_interval_seconds())
                continue
            result = await capture_intraday_order_book_snapshot([str(row["symbol"]) for row in rows])
            if result.get("status") == "failed":
                print(f"intraday order-book capture failed: {result.get('reason', '')[:300]}")
        await asyncio.sleep(intraday_order_book_interval_seconds())


async def capture_intraday_minute_sessions(symbols: list[str]) -> dict[str, Any]:
    return await _intraday_minute_capture_actions.capture(
        symbols,
        realtime_session=realtime_market_session_async,
        fetch_minutes=tencent_intraday_minutes,
        run_database=run_database_blocking,
        parse_minute=offline_minute_row,
        ensure_instrument=ensure_offline_instrument,
        retention_days=intraday_minute_profile_retention_days,
    )


async def intraday_tencent_surge_context(
    watches: list[dict[str, Any]], *, mapped_peers: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch a small opt-in target/peer basket for research peer breadth."""
    requested: list[str] = []
    mapped_peers = mapped_peers or {}
    # The capped minute basket is decision evidence, not an alphabetical
    # sample.  Strategy targets and their explicit peer contracts must be
    # scheduled before passive watches and inferred member relations.
    configured_targets: list[str] = []
    configured_peers: list[str] = []
    passive_watches: list[str] = []
    mapped_peer_symbols: list[str] = []
    def append_unique(bucket: list[str], value: Any) -> None:
        symbol = str(value).upper()
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) and symbol not in bucket:
            bucket.append(symbol)
    for watch in watches:
        watch_symbol = str(watch["symbol"]).upper()
        metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
        configurations = [metadata.get(key) for key in ("surge_strategy", "reversal_research", "upside_research")
                          if isinstance(metadata.get(key), dict) and metadata[key].get("enabled")]
        if configurations:
            append_unique(configured_targets, watch_symbol)
        else:
            append_unique(passive_watches, watch_symbol)
        mapped_values = (mapped_peers.get(watch_symbol) or {}).get("peer_symbols") or []
        for strategy in configurations:
            for value in strategy.get("peer_symbols") or []:
                append_unique(configured_peers, value)
        for value in mapped_values:
            append_unique(mapped_peer_symbols, value)
    for bucket in (configured_targets, configured_peers, passive_watches, mapped_peer_symbols):
        for symbol in bucket:
            if symbol not in requested:
                requested.append(symbol)
    # A public minute endpoint is corroborating evidence, not a broad scanner.
    # The cap is the audited explicit-watch capacity, never an implicit prefix.
    requested_total = len(requested)
    requested = requested[:intraday_minute_profile_max_symbols()]
    # One-minute bars do not gain information every ten seconds.  Keep a tiny,
    # expiring cache so the high-frequency quote loop does not turn a bounded
    # research basket into repeated public-provider scraping.  It contains at
    # most the explicit peer basket and is pruned on every use.
    now_monotonic = asyncio.get_running_loop().time()
    cache_ttl_seconds = 45.0
    for cached_symbol, cached in list(_intraday_tencent_minute_cache.items()):
        if now_monotonic - cached[0] > cache_ttl_seconds * 4:
            _intraday_tencent_minute_cache.pop(cached_symbol, None)
    cached_features: dict[str, dict[str, Any]] = {}
    cached_errors: dict[str, str] = {}
    missing: list[str] = []
    for symbol in requested:
        cached = _intraday_tencent_minute_cache.get(symbol)
        if cached is not None and now_monotonic - cached[0] <= cache_ttl_seconds:
            if cached[1] is not None:
                cached_features[symbol] = cached[1]
            elif cached[2]:
                cached_errors[symbol] = cached[2]
        else:
            missing.append(symbol)
    if missing and TENCENT_INTRADAY_MINUTE_CAPABILITY in await open_provider_capabilities(
        "tencent_free", [TENCENT_INTRADAY_MINUTE_CAPABILITY],
    ):
        errors = {**cached_errors, **{symbol: "provider health circuit is open; upstream request skipped" for symbol in missing}}
        return cached_features, {"requested": requested, "requested_total": requested_total,
                                 "truncated": requested_total > len(requested),
                                 "completed": sorted(cached_features), "errors": errors,
                                 "cached_symbols": sorted(cached_features), "cache_ttl_seconds": cache_ttl_seconds,
                                 "provider_status": "circuit_open"}
    semaphore = asyncio.Semaphore(8)
    async def fetch_one(symbol: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            async with semaphore:
                rows = await asyncio.wait_for(tencent_intraday_minutes(symbol), timeout=6)
            return symbol, intraday_minute_features(rows, source="tencent_free_minute"), None
        except (asyncio.TimeoutError, httpx.HTTPError, FreeProviderError, ValueError) as error:
            return symbol, None, str(error)[:240]
    started_at = asyncio.get_running_loop().time()
    # A slow public minute endpoint must not stretch the 10/30-second quote
    # loop.  Persist completed partial evidence, cancel only the unfinished
    # coroutines, and mark the omission explicitly in the scan provenance.
    tasks: dict[asyncio.Task[tuple[str, dict[str, Any] | None, str | None]], str] = {}
    pending: set[asyncio.Task[tuple[str, dict[str, Any] | None, str | None]]] = set()
    results: list[tuple[str, dict[str, Any] | None, str | None]] = []
    if missing:
        tasks = {asyncio.create_task(fetch_one(symbol)): symbol for symbol in missing}
        done, pending = await asyncio.wait(tasks, timeout=6.5)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            try:
                results.append(task.result())
            except Exception as error:  # noqa: BLE001 - converted into per-symbol evidence
                results.append((tasks[task], None, safe_error_detail(str(error), 240)))
        results.extend((tasks[task], None, "minute_context_deadline_exceeded") for task in pending)
    features = dict(cached_features)
    errors = dict(cached_errors)
    fresh_errors: list[str] = []
    fresh_completed = 0
    for symbol, item, error in results:
        _intraday_tencent_minute_cache[symbol] = (now_monotonic, item, error)
        if item is not None:
            features[symbol] = item
            fresh_completed += 1
        elif error:
            errors[symbol] = error
            fresh_errors.append(error)
    if missing:
        await run_database_blocking(
            persist_tencent_intraday_minute_health, fresh_completed, fresh_errors,
            round((asyncio.get_running_loop().time() - started_at) * 1000),
        )
    return features, {"requested": requested, "requested_total": requested_total,
                      "truncated": requested_total > len(requested),
                      "completed": sorted(features), "errors": errors,
                      "cached_symbols": sorted(cached_features), "cache_ttl_seconds": cache_ttl_seconds,
                      "priority": {"configured_targets": configured_targets,
                                   "configured_peers": configured_peers,
                                   "passive_watches": passive_watches,
                                   "mapped_peers": mapped_peer_symbols},
                      "deadline_exceeded_symbols": sorted(tasks[task] for task in pending),
                      "provider_status": "completed" if fresh_completed else "failed" if fresh_errors else "cached"}


async def intraday_board_cache_evidence(observed_at: datetime) -> dict[str, Any]:
    """Expose the latest persisted board-flow age without refetching it per quote scan."""
    def load() -> Any:
        with db.transaction() as connection:
            return connection.execute(
                """SELECT observed_at,status FROM quant.intraday_board_reports
                     WHERE status='completed' ORDER BY observed_at DESC LIMIT 1"""
            ).fetchone()
    row = await run_database_blocking(load)
    if row is None:
        return {"status": "missing", "notice": "no persisted board-flow snapshot yet"}
    age_seconds = max(0.0, (observed_at - row["observed_at"]).total_seconds())
    return {"status": "cached", "observed_at": row["observed_at"].isoformat(),
            "age_seconds": round(age_seconds, 1),
            "notice": "Eastmoney board flow is a cached snapshot, not a tick-by-tick feed"}


def intraday_fast_quote_confirmation(quote: dict[str, Any] | None, fast_quote: dict[str, Any] | None,
                                     observed_at: datetime, max_age_seconds: float = 30.0) -> dict[str, Any]:
    """Compare Tencent with the latest rotating Super GET ``rt_k`` sample.

    ``rt_k`` has no exchange timestamp, so freshness comes from our persisted
    observation time. Missing or stale evidence does not veto a signal. A
    fresh material disagreement does, preventing a bad cross-source quote from
    reaching Feishu as a confirmed strategy alert.
    """
    return cross_source_confirmation(
        quote, fast_quote, observed_at, max_age_seconds,
        number=intraday_number,
    )


async def latest_intraday_fast_quote_confirmations(symbols: list[str], quotes: dict[str, dict[str, Any]],
                                                   observed_at: datetime) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    def load() -> list[Any]:
        with db.transaction() as connection:
            return connection.execute(
                """SELECT DISTINCT ON(symbol) symbol,observed_at,price,pct_change,raw
                     FROM quant.intraday_quote_observations
                    WHERE source_name='tushare_super_get_rt_k' AND symbol=ANY(%s)
                    ORDER BY symbol,observed_at DESC""",
                (symbols,),
            ).fetchall()
    rows = await run_database_blocking(load)
    latest = {str(row["symbol"]): dict(row) for row in rows}
    return {symbol: intraday_fast_quote_confirmation(quotes.get(symbol), latest.get(symbol), observed_at)
            for symbol in symbols}


def persist_intraday_scan_signals(scan_id: uuid.UUID, observed_at: datetime, selected_symbols: list[str],
                                  source_status: dict[str, Any], watches: list[dict[str, Any]],
                                  quotes: dict[str, dict[str, Any]], tencent_rows: list[dict[str, Any]],
                                  quote_latency_ms: int, tushare_minutes: dict[str, dict[str, Any]],
                                  surge_features: dict[str, dict[str, Any]],
                                  peer_contexts: dict[str, dict[str, Any]],
                                  fast_confirmations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate and persist one scan's evidence in a single synchronous transaction.

    The caller runs this function in ``database_executor``.  Keeping the
    original one-transaction boundary preserves the signal de-duplication and
    point-in-time context semantics while avoiding event-loop blocking.
    """
    signals: list[dict[str, Any]] = []
    with db.transaction() as connection:
        local_trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        roll_paper_positions_sellable(connection, trading_date=local_trade_date)
        # This health capability describes the all-A cross-section.  Never
        # record a timeout/empty snapshot as a successful Tencent full-market
        # poll merely because a separate watch-price fallback kept the scan
        # alive.
        if tencent_rows:
            record_provider_success(connection, "tencent_free", "realtime_quote", len(tencent_rows), quote_latency_ms)
        else:
            record_provider_failure(connection, "tencent_free", "realtime_quote",
                                    "all-A Tencent snapshot unavailable during watch scan", quote_latency_ms)
        connection.execute(
            """INSERT INTO quant.intraday_scan_runs(scan_id,observed_at,status,requested_symbols,source_status,summary)
               VALUES(%s,%s,'completed',%s,%s,%s)""",
            (scan_id, observed_at, Json(selected_symbols), Json(strategy_json_safe(source_status)),
            Json({"watched": len(watches)})),
        )
        account = connection.execute(
            "SELECT cash FROM quant.paper_accounts WHERE account_key='default'"
        ).fetchone()
        prior_snapshot = connection.execute(
            "SELECT equity,payload FROM quant.paper_portfolio_snapshots ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        prior_payload = dict(prior_snapshot["payload"] or {}) if prior_snapshot else {}
        # Cash is only an explicit paper account value; never infer it from
        # quotes or an unconfigured portfolio snapshot.
        persist_portfolio_snapshot(
            connection, as_of=observed_at, quotes=quotes,
            cash=float(account["cash"]) if account is not None else 0,
            previous_equity=float(prior_snapshot["equity"]) if prior_snapshot and prior_snapshot["equity"] is not None else None,
            previous_close_equity=float(prior_payload.get("previous_close_equity") or 0) or None,
        )
        session_start = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).astimezone(timezone.utc)
        order_book_rows = connection.execute(
            """SELECT symbol,observed_at,raw FROM quant.intraday_quote_observations
                 WHERE symbol=ANY(%s) AND source_name='tencent_order_book'
                   AND observed_at>=%s AND observed_at<%s
                 ORDER BY symbol,observed_at DESC""",
            (selected_symbols, max(session_start, observed_at - timedelta(minutes=5)), observed_at),
        ).fetchall()
        order_book_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for item in order_book_rows:
            order_book_by_symbol.setdefault(str(item["symbol"]), []).append(dict(item))
        clear_stale_signal_episodes(connection, selected_symbols, observed_at)
        market_contexts = intraday_point_in_time_market_context_batch(
            connection, [(observed_at, symbol) for symbol in selected_symbols],
        )
        paper_positions = {
            str(row["symbol"]): dict(row)
            for row in connection.execute(
                "SELECT symbol,quantity,sellable_quantity,average_cost FROM quant.paper_positions WHERE symbol=ANY(%s)",
                (selected_symbols,),
            ).fetchall()
        }
        sector_rows = connection.execute(
            """SELECT symbol,sector_key FROM quant.sector_membership_history
                WHERE symbol=ANY(%s) AND effective_from<=%s
                  AND (effective_to IS NULL OR effective_to>=%s)
                  AND taxonomy_key IN ('ths_concept_flow','ths_index_n','ths_industry')""",
            (selected_symbols, local_trade_date, local_trade_date),
        ).fetchall() if selected_symbols else []
        candidate_sector_keys: dict[str, list[str]] = {}
        for row in sector_rows:
            candidate_sector_keys.setdefault(str(row["symbol"]), []).append(str(row["sector_key"]))
        paper_snapshot = connection.execute(
            "SELECT drawdown,payload FROM quant.paper_portfolio_snapshots ORDER BY as_of DESC LIMIT 1",
        ).fetchone()
        snapshot_payload = dict(paper_snapshot["payload"] or {}) if paper_snapshot else {}
        if paper_snapshot:
            snapshot_payload["drawdown"] = paper_snapshot["drawdown"]
        shadow_priors = latest_shadow_priors_v2(connection)
        rebound_priors = latest_rebound_priors(connection)
        probability_profiles = load_intraday_probability_profiles(connection)
        daily_factors_by_symbol = pure_watchlist_daily_factors_by_symbol(
            selected_symbols, connection, number=intraday_number,
        )
        raw_minute_features_by_symbol = {
            symbol: (tushare_minutes.get(symbol) or {}).get("feature") or surge_features.get(symbol)
            for symbol in selected_symbols
        }
        minute_volume_profiles_by_symbol = pure_intraday_volume_time_profiles(
            {
                symbol: (feature or {}).get("time")
                for symbol, feature in raw_minute_features_by_symbol.items()
                if feature is not None
            },
            local_trade_date,
            connection,
            minute_bucket_fn=intraday_minute_bucket,
            number=intraday_number,
        )
        quote_sources = {
            str(watch["symbol"]): intraday_quote_observation_source(quotes.get(str(watch["symbol"])))
            for watch in watches
        }
        previous_by_symbol = previous_quote_frames(
            connection, quote_sources,
            not_before=max(session_start, observed_at - timedelta(seconds=15)),
            observed_at=observed_at,
        )
        first_eac_by_symbol = first_eac_breakout_events(
            connection, selected_symbols,
            not_before=observed_at - INTRADAY_CONFIRMATION_WINDOW,
        )
        for watch in watches:
            symbol = str(watch["symbol"])
            quote = quotes.get(symbol)
            quote_source_name = intraday_quote_observation_source(quote)
            previous = previous_by_symbol.get(symbol)
            if quote:
                quote_raw = dict(quote.get("raw") or {})
                quote_raw["_observation_source"] = quote_source_name
                quote_raw["_price_source"] = quote.get("price_source")
                connection.execute(
                    """INSERT INTO quant.intraday_quote_observations(scan_id,symbol,observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (scan_id, symbol, observed_at, quote_source_name, quote.get("price"), quote.get("pct_change"),
                     quote.get("volume_ratio"), quote.get("turnover_rate"), quote.get("main_net_inflow"),
                     Json(strategy_json_safe(quote_raw))),
                )
            daily_factors = daily_factors_by_symbol.get(symbol, {"status": "insufficient_history", "bar_count": 0})
            minute_feature = pure_attach_volume_time_profile(
                raw_minute_features_by_symbol.get(symbol), minute_volume_profiles_by_symbol.get(symbol),
                number=intraday_number,
            )
            order_book_feature = aggregate_order_book_observations(order_book_by_symbol.get(symbol, []), observed_at)
            peer_context = peer_contexts.get(symbol)
            previous_quote = dict(previous) if previous else None
            # Freeze all pre-confirmation inputs before rules emit.  This
            # lets future replay evaluate the exact same policy/risk gate from
            # local evidence, without consulting the then-current board,
            # quote or paper ledger.
            fast_confirmation = fast_confirmations.get(symbol, {"status": "missing", "max_age_seconds": 30})
            market_context = market_contexts.get((observed_at, symbol), {})
            portfolio_context = {
                "position": paper_positions.get(symbol) or {},
                "snapshot": snapshot_payload,
                "candidate_sector_keys": candidate_sector_keys.get(symbol, ()),
            }
            persist_rule_input_snapshot(
                connection, scan_id=scan_id, observed_at=observed_at, watch=watch, quote=quote,
                previous_quote=previous_quote, daily_factors=daily_factors, minute_features=minute_feature,
                peer_context=peer_context, model_version=INTRADAY_SIGNAL_MODEL_VERSION,
                market_context=market_context, fast_confirmation=fast_confirmation,
                portfolio_context=portfolio_context,
            )
            rule_quote = {**quote, "_scan_observed_at": observed_at} if quote else None
            generated_signals = intraday_signal_rules(watch, rule_quote, previous_quote, daily_factors,
                                                       minute_feature, peer_context)
            shadow_signal = main_wave_v2_shadow_signal(
                watch, quote, minute_feature, peer_context, shadow_priors.get(symbol),
            )
            if shadow_signal is not None:
                generated_signals.append(shadow_signal)
            rebound_signal = countertrend_rebound_realtime_signal(
                watch, quote, minute_feature, peer_context, rebound_priors.get(symbol),
            )
            if rebound_signal is not None:
                generated_signals.append(rebound_signal)
            rebound_failure_signal = countertrend_rebound_failure_reduce_signal(
                watch, quote, minute_feature, peer_context, rebound_priors.get(symbol),
            )
            if rebound_failure_signal is not None:
                generated_signals.append(rebound_failure_signal)
            first_eac = first_eac_by_symbol.get(symbol)
            if first_eac is not None:
                acceptance = intraday_eac_acceptance_assessment(
                    dict(first_eac["conditions"] or {}), first_observed_at=first_eac["observed_at"],
                    observed_at=observed_at, quote=quote, previous_quote=previous_quote,
                    minute_features=minute_feature, peer_context=peer_context,
                )
                if acceptance["status"] in {"candidate", "attention_only"}:
                    entry_class = acceptance["status"] == "candidate"
                    generated_signals.append({
                        "symbol": symbol,
                        "signal_key": (f"{symbol}:entry:upside_acceptance_eac_v4" if entry_class
                                       else f"{symbol}:watch:upside_acceptance_attention_v4"),
                        "signal_type": "entry" if entry_class else "watch",
                        "severity": "warning" if entry_class else "info",
                        "score": acceptance["score"], "hard": False,
                        "independent_confirmation": True, "stage_upgrade": True,
                        "conditions": {"price": (quote or {}).get("price"),
                                       "pct_change": (quote or {}).get("pct_change"),
                                       "volume_ratio": (quote or {}).get("volume_ratio"),
                                       "turnover_rate": (quote or {}).get("turnover_rate"),
                                       "main_net_inflow": (quote or {}).get("main_net_inflow"),
                                       "setup": "eac_acceptance_confirmed",
                                       "eac_state": acceptance["status"],
                                       "eac_acceptance_assessment": acceptance,
                                       "minute_features": minute_feature or {"status": "not_available"},
                                       "peer_context": peer_context or {"status": "not_available"}},
                        "risk_flags": ["eac_timed_acceptance", "manual_review_required", "no_automatic_order",
                                       *acceptance.get("risk_flags", [])],
                    })
            for signal in generated_signals:
                # Signal rules historically identify the symbol in signal_key;
                # normalize the outer symbol before paper/audit persistence so
                # every rule (including EAC acceptance) satisfies the payload
                # contract.
                signal.setdefault("symbol", symbol)
                signal.setdefault("observed_at", observed_at)
                signal["conditions"] = {**signal["conditions"], "realtime_cross_check": fast_confirmation}
                if fast_confirmation.get("status") == "mismatch":
                    signal["risk_flags"] = [*signal["risk_flags"], "realtime_cross_source_price_mismatch"]
            for signal in generated_signals:
                portfolio_gate = paper_risk_gate(
                    signal_type=signal["signal_type"], symbol=symbol,
                    position=paper_positions.get(symbol),
                    snapshot=snapshot_payload,
                    candidate_sector_keys=candidate_sector_keys.get(symbol, ()),
                )
                portfolio_risk = {"allowed": portfolio_gate.allowed, "target_weight": portfolio_gate.target_weight,
                                  "reasons": list(portfolio_gate.reasons), "risk_flags": list(portfolio_gate.risk_flags)}
                policy = live_policy_gate(signal, watch, quote, daily_factors, market_context, fast_confirmation,
                                          portfolio_risk)
                setup_state = classify_intraday_setup_state(
                    watch, quote, minute_feature, peer_context, policy,
                )
                signal["conditions"] = {
                    **signal["conditions"], "policy_gate": policy,
                    "setup_state": setup_state,
                    # Persist the bounded aggregate rather than raw book
                    # frames.  Its registered contract is attribution-only:
                    # this records a one-sided seal/queue observation for
                    # later replay without changing a live score or entry.
                    "order_book_proxy": order_book_feature,
                    "factor_contract_version": INTRADAY_FACTOR_CONTRACT_VERSION,
                    "factor_contracts": intraday_factor_contracts_for_signal(signal),
                }
                signal["risk_flags"] = [*signal["risk_flags"], *policy["risk_flags"]]
                probability = intraday_probability_for_signal(signal, probability_profiles)
                signal["conditions"] = {
                    **signal["conditions"],
                    "decision_context": intraday_decision_context(signal, probability),
                }
                signal["conditions"] = {
                    **signal["conditions"],
                    "signal_contract": intraday_signal_contract(signal, observed_at),
                }
                latest = connection.execute(
                    "SELECT observed_at FROM quant.intraday_signal_events WHERE signal_key=%s ORDER BY observed_at DESC LIMIT 1",
                    (signal["signal_key"],),
                ).fetchone()
                last_key_alerted = connection.execute(
                    """SELECT observed_at,score,conditions FROM quant.intraday_signal_events
                         WHERE signal_key=%s AND state='alerted' AND observed_at>=%s
                         ORDER BY observed_at DESC LIMIT 1""",
                    (signal["signal_key"], session_start),
                ).fetchone()
                last_symbol_watch_alerted = connection.execute(
                    """SELECT observed_at FROM quant.intraday_signal_events
                         WHERE symbol=%s AND signal_type='watch' AND state='alerted'
                         ORDER BY observed_at DESC LIMIT 1""",
                    (symbol,),
                ).fetchone()
                state = intraday_signal_event_state(
                    signal, observed_at=observed_at,
                    latest_event_at=latest["observed_at"] if latest else None,
                    last_key_alerted_at=last_key_alerted["observed_at"] if last_key_alerted else None,
                    last_symbol_watch_alerted_at=last_symbol_watch_alerted["observed_at"] if last_symbol_watch_alerted else None,
                    last_key_alert=dict(last_key_alerted) if last_key_alerted else None,
                )
                if signal.get("shadow_only"):
                    state = "suppressed"
                if state == "confirmed" and fast_confirmation.get("status") == "mismatch":
                    state = "confirming"
                if state == "confirmed" and not policy["allow_confirmation"]:
                    state = "confirming"
                episode = None if signal["signal_type"] == "data_issue" else ensure_signal_episode(
                    connection, signal, observed_at, state, symbol=symbol,
                )
                signal["conditions"] = {**signal["conditions"], "episode": episode or {"state": "not_applicable"}}
                evidence = {"tencent": quote, "tencent_order_book": order_book_feature, "tencent_minute": minute_feature,
                            "peer_context": peer_context, "tushare_rt_min": tushare_minutes.get(symbol),
                            "tushare_rt_k_fast": fast_confirmation,
                            "daily_factors": daily_factors, "market_context": market_context}
                evidence["attribution"] = intraday_signal_attribution(
                    signal["signal_key"], signal["signal_type"], signal["conditions"], evidence, market_context,
                )
                event = connection.execute(
                    """INSERT INTO quant.intraday_signal_events(
                         scan_id,symbol,signal_key,signal_type,severity,state,score,observed_at,expires_at,
                         conditions,evidence,risk_flags,episode_id,material_state_hash,stage)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING signal_event_id""",
                    (scan_id, symbol, signal["signal_key"], signal["signal_type"], signal["severity"], state,
                     signal["score"], observed_at, observed_at + INTRADAY_CONFIRMATION_WINDOW,
                     Json(signal["conditions"]), Json(evidence), Json(signal["risk_flags"]),
                     episode["episode_id"] if episode else None,
                     episode["material_state_hash"] if episode else None,
                     episode["stage"] if episode else "data_issue"),
                ).fetchone()
                # Confirmed signals create an auditable paper proposal only.
                # No broker client or live order path is reachable here.
                if state == "confirmed":
                    paper_payload = paper_decision_payload(
                        signal, state, policy,
                        {"allowed": portfolio_gate.allowed, "target_weight": portfolio_gate.target_weight,
                         "reasons": portfolio_gate.reasons, "risk_flags": portfolio_gate.risk_flags},
                    )
                    persist_paper_decision(
                        connection, event["signal_event_id"], paper_payload,
                    )
                    if not portfolio_gate.allowed:
                        connection.execute(
                            """INSERT INTO quant.paper_risk_events(decision_id,symbol,event_type,severity,message,occurred_at,details)
                               SELECT decision_id,%s,'portfolio_limit','block',%s,%s,%s::jsonb
                                 FROM quant.paper_decisions
                                WHERE signal_event_id=%s ORDER BY created_at DESC LIMIT 1""",
                            (symbol, "; ".join(portfolio_gate.reasons), observed_at,
                             Json({"risk_flags": list(portfolio_gate.risk_flags)}), event["signal_event_id"]),
                        )
                signals.append({"signal_event_id": event["signal_event_id"], "symbol": symbol, "state": state,
                                **signal, "observed_at": observed_at, "quote": quote,
                                "minute": (tushare_minutes.get(symbol) or {}).get("latest"),
                                "fast_quote_confirmation": fast_confirmation, "watch": watch})
    return signals


intraday_rule_input_pruned_on: date | None = None


async def prune_intraday_rule_input_evidence_if_due(observed_at: datetime) -> None:
    """Run one bounded evidence-retention pass per China trading date.

    Frozen core inputs are retained for replay, while repeated non-confirmed
    signal rows are retained for the same conservative window and then safely
    removed only when they have neither an alert delivery nor an outcome.
    """
    global intraday_rule_input_pruned_on
    local_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if intraday_rule_input_pruned_on == local_date:
        return
    rule_input_cutoff = observed_at - timedelta(days=intraday_rule_input_retention_days())
    event_cutoff = observed_at - timedelta(days=ephemeral_signal_retention_days())

    def prune() -> None:
        with db.transaction() as connection:
            prune_rule_input_evidence(connection, cutoff=rule_input_cutoff)
            prune_ephemeral_signal_events(connection, cutoff=event_cutoff)

    await run_database_blocking(prune)
    intraday_rule_input_pruned_on = local_date


async def run_intraday_watchlist_scan(request: IntradayScanRequest) -> dict[str, Any]:
    """Persist a bounded live scan.  The endpoint does not submit orders."""
    scan_started_at = asyncio.get_running_loop().time()

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        intraday_scan_duration_seconds.labels(str(payload.get("status") or "unknown")).observe(
            max(0.0, asyncio.get_running_loop().time() - scan_started_at)
        )
        return payload

    observed_at = datetime.now(timezone.utc)
    active, reason = await realtime_market_session_async()
    def load_watches() -> list[Any]:
        with db.transaction() as connection:
            if request.symbols:
                return connection.execute("SELECT * FROM quant.intraday_watchlists WHERE enabled AND symbol=ANY(%s) ORDER BY symbol", (request.symbols,)).fetchall()
            # Fetch one extra row only to detect overflow.  It is unsafe to
            # quietly scan the first 40 while presenting the result as a full
            # watchlist decision.
            return connection.execute("SELECT * FROM quant.intraday_watchlists WHERE enabled ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT 41").fetchall()
    rows = await run_database_blocking(load_watches)
    watches = [dict(row) for row in rows]
    selected_symbols = [str(row["symbol"]) for row in watches]
    scan_id = uuid.uuid4()
    capacity = intraday_watchlist_capacity(len(watches))
    if capacity["blocked"]:
        await run_database_blocking(
            persist_intraday_scan_terminal, db, scan_id, observed_at, "blocked", request.symbols,
            {"watchlist_capacity": capacity}, {"watched": len(watches)},
        )
        return finish({
            "status": "blocked", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(),
            "reason": capacity["reason"], "watchlist_capacity": capacity, "alerts": [],
        })
    if not active:
        await run_database_blocking(
            persist_intraday_scan_terminal, db, scan_id, observed_at, "blocked", request.symbols,
            {"session": reason}, {"watched": len(watches)},
        )
        return finish({"status": "blocked", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(), "reason": reason, "alerts": []})
    await prune_intraday_rule_input_evidence_if_due(observed_at)
    retry_summary = await retry_pending_intraday_alerts()
    if not watches:
        await run_database_blocking(
            persist_intraday_scan_terminal, db, scan_id, observed_at, "completed", request.symbols,
            {"tencent": "skipped"}, {"watched": 0},
        )
        return finish({"status": "completed", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(), "alerts": [],
                       "notice": "没有启用的观察/持仓标的；先通过 watchlists API 显式添加。"})

    def load_exact_watchlist_memberships() -> list[dict[str, Any]]:
        """Load only point-in-time relations for the explicit watchlist.

        The peer helper groups these rows by the exact taxonomy/sector pair;
        no human-readable label matching and no full-sector enumeration occur
        on the live scan path.
        """
        local_trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        with db.transaction() as connection:
            rows = connection.execute(
                """SELECT taxonomy_key,sector_key,symbol
                     FROM quant.sector_membership_history
                    WHERE symbol=ANY(%s) AND effective_from<=%s
                      AND (effective_to IS NULL OR effective_to>=%s)
                      AND taxonomy_key IN ('ths_concept_flow','ths_index_n','ths_industry')""",
                (selected_symbols, local_trade_date, local_trade_date),
            ).fetchall()
        return [dict(row) for row in rows]

    membership_rows = await run_database_blocking(load_exact_watchlist_memberships)
    mapped_peer_groups = pure_mapped_watchlist_peers(selected_symbols, membership_rows)
    quote_started_at = asyncio.get_running_loop().time()
    all_a_task = asyncio.create_task(intraday_all_a_snapshot())
    all_a_task.add_done_callback(consume_background_task_exception)
    watch_quote_task = asyncio.create_task(tencent_order_book_quotes(selected_symbols, max_symbols=40))
    try:
        fresh_watch_rows = await watch_quote_task
    except (httpx.HTTPError, FreeProviderError, ValueError):
        fresh_watch_rows = []
    try:
        sina_watch_rows = await sina_quotes(selected_symbols) if not fresh_watch_rows else []
    except (httpx.HTTPError, FreeProviderError, ValueError):
        sina_watch_rows = []
    try:
        # A fresh all-A snapshot is valuable for flow percentiles, but never
        # allowed to delay the explicit watchlist beyond this small budget.
        tencent_rows, all_a_snapshot_status = await asyncio.wait_for(asyncio.shield(all_a_task), timeout=2.0)
    except ExecutorSaturatedError as error:
        detail = safe_error_detail(str(error), 300)
        tencent_rows, all_a_snapshot_status = [], {"status": "unavailable", "error": detail}
    except (asyncio.TimeoutError, AkShareProviderError, ValueError) as error:
        detail = safe_error_detail(str(error), 300)
        tencent_rows, all_a_snapshot_status = [], {"status": "unavailable", "error": detail}
    quotes = {item["symbol"]: item for row in tencent_rows if (item := intraday_quote_from_tencent(row)) is not None}
    eastmoney_watch_flow_rows: list[dict[str, Any]] = []
    if not tencent_rows:
        try:
            eastmoney_watch_flow_rows = await asyncio.wait_for(
                eastmoney_watch_flow_quotes(selected_symbols, max_symbols=40), timeout=2.0,
            )
        except (asyncio.TimeoutError, httpx.HTTPError, FreeProviderError, ValueError) as error:
            all_a_snapshot_status = {**all_a_snapshot_status, "eastmoney_watch_fallback_error": safe_error_detail(str(error), 300)}
        else:
            if eastmoney_watch_flow_rows:
                merge_intraday_eastmoney_watch_flows(quotes, eastmoney_watch_flow_rows)
                all_a_snapshot_status = {
                    "status": "fresh", "age_seconds": 0.0,
                    "source": "eastmoney_watch_flow_batch", "scope": "explicit_watchlist_only",
                    "cross_sectional": False,
                    "semantics": "watchlist_public_flow_proxy_not_exchange_order_flow",
                    "fallback_from": "tencent_all_a_snapshot",
                    "matched_symbols": len(eastmoney_watch_flow_rows),
                }
    if all_a_snapshot_status.get("cross_sectional", True):
        annotate_intraday_flow_percentiles(quotes)
    pure_annotate_flow_snapshot_provenance(quotes, all_a_snapshot_status)
    # One batch refreshes all explicit watches each scan while the slower all-A
    # cross-section is reused only for percentile normalization.
    merge_intraday_watch_quote_prices(quotes, fresh_watch_rows)
    merge_intraday_sina_watch_quotes(quotes, sina_watch_rows)
    quote_timestamp_slo_seconds = 20.0 if intraday_high_frequency_window(observed_at) else 45.0
    for quote in quotes.values():
        quote["price_freshness"] = intraday_quote_exchange_time_status(
            quote, observed_at, quote_timestamp_slo_seconds,
        )
    surge_features, surge_source = await intraday_tencent_surge_context(watches, mapped_peers=mapped_peer_groups)
    surge_source["exact_watchlist_peer_mapping"] = {
        "status": "completed", "membership_rows": len(membership_rows),
        "symbols_with_mapped_peers": sum(bool(item.get("peer_symbols")) for item in mapped_peer_groups.values()),
        "taxonomy_scope": ["ths_concept_flow", "ths_index_n", "ths_industry"],
        "notice": "仅以同一 taxonomy_key + sector_key 的观察池成员确认；不按名称猜板块关联。",
    }
    peer_contexts: dict[str, dict[str, Any]] = {}
    for watch in watches:
        symbol = str(watch["symbol"]).upper()
        metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
        configurations = [metadata.get(key) for key in ("surge_strategy", "reversal_research", "upside_research")
                          if isinstance(metadata.get(key), dict) and metadata[key].get("enabled")]
        configured_peers = [
            str(value).upper() for strategy in configurations for value in strategy.get("peer_symbols") or []
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(value).upper()) and str(value).upper() != symbol
        ]
        mapped = mapped_peer_groups.get(symbol) or {"peer_symbols": [], "groups": []}
        peers = sorted(set(configured_peers) | set(mapped.get("peer_symbols") or []))
        context = intraday_peer_context(peers, surge_features)
        peer_contexts[symbol] = {
            **context,
            "configured_peer_symbols": sorted(set(configured_peers)),
            "mapped_peer_symbols": list(mapped.get("peer_symbols") or []),
            "exact_membership_groups": list(mapped.get("groups") or []),
        }
    ordered_priority_symbols = [str(row["symbol"]) for row in sorted(watches, key=intraday_watch_priority_key)]
    priority_symbols, next_realtime_validation_offset = intraday_realtime_validation_slice(
        ordered_priority_symbols,
        request.realtime_validation_offset,
        request.realtime_validation_limit,
    )
    tushare_minutes = await intraday_tushare_minutes(priority_symbols) if priority_symbols else {}
    fast_confirmations = await latest_intraday_fast_quote_confirmations(selected_symbols, quotes, observed_at)
    fast_status_counts: dict[str, int] = {}
    for item in fast_confirmations.values():
        status = str(item.get("status") or "unknown")
        fast_status_counts[status] = fast_status_counts.get(status, 0) + 1
    board_cache_evidence = await intraday_board_cache_evidence(observed_at)
    direct_watch_symbols = {
        str(row.get("ts_code") or "") for row in fresh_watch_rows
        if str(row.get("ts_code") or "") in selected_symbols
    }
    sina_watch_symbols = {
        str(row.get("ts_code") or "") for row in sina_watch_rows
        if str(row.get("ts_code") or "") in selected_symbols
    }
    all_a_watch_symbols = {
        symbol for symbol in selected_symbols
        if (quotes.get(symbol) or {}).get("price_source") == "tencent_all_a_snapshot"
    }
    direct_watch_count = len(direct_watch_symbols)
    fresh_direct_watch_count = sum(
        1 for symbol in direct_watch_symbols
        if ((quotes.get(symbol) or {}).get("price_freshness") or {}).get("status") == "fresh"
    )
    tencent_status = ("completed" if fresh_direct_watch_count == len(selected_symbols) else
                      "partial" if direct_watch_count or tencent_rows else "unavailable")
    source_status = {"tencent": {"status": tencent_status, "rows": len(tencent_rows),
                                         "matched": sum(symbol in quotes for symbol in selected_symbols),
                                         "all_a_snapshot": all_a_snapshot_status,
                                         "fresh_watch_quote_rows": len(fresh_watch_rows),
                                         "fresh_watch_quote_symbols": direct_watch_count,
                                         "decision_eligible_watch_quote_symbols": fresh_direct_watch_count,
                                         "stale_or_unstamped_direct_watch_quote_symbols": direct_watch_count - fresh_direct_watch_count,
                                         "quote_timestamp_slo_seconds": quote_timestamp_slo_seconds,
                                         "all_a_only_watch_quote_symbols": len(all_a_watch_symbols),
                                         "sina_fallback_watch_quote_symbols": len(sina_watch_symbols),
                                         "missing_direct_watch_quote_symbols": len(selected_symbols) - direct_watch_count,
                                         "sina_watch_quote_rows": len(sina_watch_rows)},
                     "eastmoney_watch_flow": {"status": "completed" if eastmoney_watch_flow_rows else "not_used",
                                                "rows": len(eastmoney_watch_flow_rows),
                                                "scope": "explicit_watchlist_only",
                                                "percentiles": "not_computed"},
                     "tencent_minute_context": surge_source,
                     "tushare_rt_min": {
                         "requested": priority_symbols,
                         "items": {symbol: item["source"] for symbol, item in tushare_minutes.items()},
                         "rotation_pool_size": len(ordered_priority_symbols),
                         "rotation_start_offset": (
                             request.realtime_validation_offset % len(ordered_priority_symbols)
                             if ordered_priority_symbols else 0
                         ),
                         "next_rotation_offset": next_realtime_validation_offset,
                     },
                     "tushare_rt_k_fast": {"status_counts": fast_status_counts, "max_age_seconds": 30,
                                             "cadence": "one request start per second in selected windows"},
                     "eastmoney_board_flow": board_cache_evidence,
                     "post_close_lhb_cninfo": "context only; never used in same-day intraday signal"}
    quote_latency_ms = round((asyncio.get_running_loop().time() - quote_started_at) * 1000)
    signals = await run_database_blocking(
        persist_intraday_scan_signals, scan_id, observed_at, selected_symbols, source_status, watches,
        quotes, tencent_rows, quote_latency_ms, tushare_minutes, surge_features, peer_contexts,
        fast_confirmations, timeout_seconds=60,
    )
    alerts: list[dict[str, Any]] = []
    for signal in signals:
        if signal["state"] != "confirmed":
            continue
        delivery = await deliver_intraday_alert(
            signal["signal_event_id"],
            intraday_alert_text(
                signal, signal["watch"], signal["quote"] or {}, signal["minute"],
                decision_card_url=decision_card_url(signal["symbol"]),
            ),
        )
        alerts.append({"signal_event_id": str(signal["signal_event_id"]), "symbol": signal["symbol"], "signal_type": signal["signal_type"],
                       "severity": signal["severity"], "delivery": delivery})
    return finish({"status": "completed", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(), "source_status": source_status,
                   "signals": [{key: value for key, value in signal.items() if key not in {"watch", "quote"}} for signal in signals], "alerts": alerts,
                   "realtime_validation": {
                       "pool_size": len(ordered_priority_symbols),
                       "requested_symbols": priority_symbols,
                       "next_offset": next_realtime_validation_offset,
                   },
                   "delivery_retry": retry_summary,
                   "notice": "仅为人工复核提醒，不构成交易指令；系统不会自动下单。"})


def intraday_board_curve_session(now: datetime | None = None) -> tuple[bool, str]:
    """Apply both the exchange clock and the persisted SSE holiday calendar."""
    observed_at = now or datetime.now(timezone.utc)
    active, reason = intraday_board_curve_clock_session(observed_at)
    if not active:
        return active, reason
    exchange_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    with db.transaction() as connection:
        calendar = connection.execute(
            "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s",
            (exchange_date,),
        ).fetchone()
    if calendar is None:
        return False, "SSE trade calendar has no entry for today; fail closed"
    if not calendar["is_open"]:
        return False, "SSE trade calendar marks today closed"
    return True, reason


async def intraday_board_curve_session_async(now: datetime | None = None) -> tuple[bool, str]:
    """Async-loop variant that keeps the calendar lookup off the event loop."""
    observed_at = now or datetime.now(timezone.utc)
    active, reason = intraday_board_curve_clock_session(observed_at)
    if not active:
        return active, reason
    exchange_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()

    def load_calendar() -> Any:
        with db.transaction() as connection:
            return connection.execute(
                "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s",
                (exchange_date,),
            ).fetchone()
    try:
        calendar = await run_database_blocking(load_calendar)
    except ExecutorSaturatedError as error:
        return False, f"local calendar capacity unavailable; fail closed: {safe_error_detail(str(error), 180)}"
    if calendar is None:
        return False, "SSE trade calendar has no entry for today; fail closed"
    if not calendar["is_open"]:
        return False, "SSE trade calendar marks today closed"
    return True, reason


async def open_provider_capabilities(provider_key: str, capabilities: list[str]) -> set[str]:
    """Read active circuit-breaker entries without issuing an upstream request."""
    if not capabilities:
        return set()

    def load() -> list[Any]:
        with db.transaction() as connection:
            return connection.execute(
                """SELECT capability FROM quant.provider_health
                     WHERE provider_key=%s AND market='cn' AND capability=ANY(%s)
                       AND circuit_open_until IS NOT NULL AND circuit_open_until > now()""",
                (provider_key, capabilities),
            ).fetchall()
    rows = await run_database_blocking(load)
    return {str(row["capability"]) for row in rows}


def intraday_board_display_slots(selected_date: date, now: datetime | None = None) -> list[datetime]:
    """Compatibility export for the board-curve read model's exchange clock grid."""
    return _board_display_slots(selected_date, now)


def intraday_board_flow_curve_items(kind: str, flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize one Eastmoney board cross-section without stock-level joins.

    The public response can repeat a display board while paginating.  One
    minute stores one value per exact upstream key/label; the median makes a
    tiny between-page timing difference deterministic without treating the
    duplicate as a second board.
    """
    if kind not in {"concept", "industry"}:
        raise ValueError("kind must be concept or industry")
    grouped: dict[tuple[str, str], list[dict[str, float | None]]] = {}
    for flow in flows:
        label = str(flow.get("行业") or flow.get("板块名称") or "").strip()
        sector_key = str(flow.get("行业代码") or flow.get("板块代码") or label).strip()
        if not label or not sector_key:
            continue
        inflow, outflow = intraday_number(flow.get("流入资金")), intraday_number(flow.get("流出资金"))
        net_inflow = inflow - outflow if inflow is not None and outflow is not None else intraday_number(flow.get("净额"))
        if net_inflow is None:
            continue
        grouped.setdefault((sector_key, label), []).append({
            "net_inflow": net_inflow,
            "change_pct": intraday_number(flow.get("行业-涨跌幅")),
        })
    items: list[dict[str, Any]] = []
    for (sector_key, label), rows in grouped.items():
        net_values = [float(row["net_inflow"]) for row in rows if row["net_inflow"] is not None]
        change_values = [float(row["change_pct"]) for row in rows if row["change_pct"] is not None]
        items.append({
            "taxonomy_key": f"eastmoney_{kind}", "sector_key": sector_key, "label": label,
            "net_inflow": round(median(net_values), 6),
            "change_pct": round(median(change_values), 6) if change_values else None,
        })
    items.sort(key=lambda item: (-float(item["net_inflow"]), str(item["sector_key"])))
    return items


async def capture_intraday_board_flow_curve() -> dict[str, Any]:
    """Capture one same-source flow point through the isolated action service."""
    return await _board_flow_capture_actions.capture(
        run_database=run_database_blocking,
        run_akshare=run_akshare_blocking,
        provider_capabilities=open_provider_capabilities,
        normalize_items=intraday_board_flow_curve_items,
        persist_feature=persist_intraday_market_flow_feature,
        evaluate_rotation=evaluate_intraday_board_rotation_events,
        retry_rotation_deliveries=retry_pending_board_rotation_alerts,
    )


def evaluate_intraday_board_rotation_events(snapshot_minute: datetime, observed_at: datetime) -> list[dict[str, Any]]:
    return _board_rotation_repository.evaluate(
        snapshot_minute, observed_at,
        candidates_for=board_rotation_candidates,
        still_directional=board_rotation_still_directional,
    )


async def deliver_board_rotation_alert(event: dict[str, Any]) -> dict[str, Any]:
    """Keep board rotation evidence in-app; never emit a chat notification."""
    return {"status": "suppressed", "reason": "Feishu is reserved for watched-stock strategy signals"}


async def retry_pending_board_rotation_alerts(limit: int = 3) -> dict[str, int]:
    """Suppress legacy board-rotation outbox rows without external delivery."""
    def suppress_legacy() -> int:
        with db.transaction() as connection:
            result = connection.execute(
                """UPDATE quant.intraday_board_rotation_deliveries
                      SET status='suppressed',error_message='suppressed: Feishu is reserved for watched-stock strategy signals',
                          next_attempt_at=NULL
                    WHERE channel='feishu_adapter' AND status IN ('pending','failed')""",
            )
        return int(result.rowcount or 0)
    suppressed = await run_database_blocking(suppress_legacy)
    return {"loaded": suppressed, "sent": 0, "failed": 0, "disabled": 0, "suppressed": suppressed}


async def intraday_board_flow_curve_loop() -> None:
    """Capture once per SSE board-observation minute without catch-up bursts."""
    completed_minute: datetime | None = None
    pruned_on: date | None = None
    while True:
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        active, _ = await intraday_board_curve_session_async()
        minute = local.replace(second=0, microsecond=0)
        if active and minute != completed_minute:
            if pruned_on != local.date():
                cutoff = datetime.now(timezone.utc) - timedelta(days=intraday_board_curve_retention_days())
                rotation_cutoff = datetime.now(timezone.utc) - timedelta(days=intraday_board_rotation_retention_days())
                def prune() -> None:
                    with db.transaction() as connection:
                        connection.execute("DELETE FROM quant.intraday_board_flow_snapshots WHERE observed_at<%s", (cutoff,))
                        # Rotation events are derived from adjacent source snapshots.
                        # Delivery receipts cascade with the event; raw snapshots,
                        # daily bars, and research evidence remain outside this cleanup.
                        connection.execute(
                            "DELETE FROM quant.intraday_board_rotation_events WHERE last_observed_at<%s",
                            (rotation_cutoff,),
                        )
                await run_database_blocking(prune)
                pruned_on = local.date()
            allowed, storage = await nonessential_high_frequency_capture_allowed()
            if not allowed:
                print(f"intraday board curve skipped by storage guard: {storage.get('state')}")
                completed_minute = minute
            else:
                try:
                    await capture_intraday_board_flow_curve()
                except Exception as error:  # noqa: BLE001 - the next minute is an independent snapshot
                    print(f"intraday board curve capture failed: {str(error)[:300]}")
            completed_minute = minute
        # Wake near the next minute boundary; never replay missed minutes.
        next_minute = (local + timedelta(minutes=1)).replace(second=1, microsecond=0)
        await asyncio.sleep(min(30.0, max(1.0, (next_minute - local).total_seconds())))


def strategy_review_automation_enabled() -> bool:
    return os.getenv("STRATEGY_REVIEW_AUTOMATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def post_close_strategy_automation_enabled() -> bool:
    return os.getenv("POST_CLOSE_STRATEGY_AUTOMATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def daily_summary_automation_enabled() -> bool:
    return os.getenv("DAILY_SUMMARY_AUTOMATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def sse_calendar_open(calendar_date: date) -> bool:
    if calendar_date.weekday() >= 5:
        return False
    with db.transaction() as connection:
        row = connection.execute("SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s", (calendar_date,)).fetchone()
    # A calendar gap must fail closed.  Treating a missing holiday row as open
    # previously ran the full post-close/intraday automation on public
    # holidays, creating avoidable provider traffic and misleading snapshots.
    return bool(row["is_open"]) if row is not None else False


async def sse_calendar_open_async(calendar_date: date) -> bool:
    """Async-loop-safe exchange-calendar gate; gaps still fail closed."""
    if calendar_date.weekday() >= 5:
        return False

    def load() -> Any:
        with db.transaction() as connection:
            return connection.execute(
                "SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s",
                (calendar_date,),
            ).fetchone()
    try:
        row = await run_database_blocking(load)
    except ExecutorSaturatedError:
        # This bool gate is used only to decide whether background work may
        # start. Capacity uncertainty must therefore suppress the round.
        return False
    return bool(row["is_open"]) if row is not None else False


async def strategy_review_loop() -> None:
    """Compose the isolated checkpoint scheduler with production side effects."""
    async def build_snapshot(exchange_date: date, session: str) -> dict[str, Any]:
        # ``MarketSnapshotRequest`` has no date override: this callback runs
        # only at the scheduler's current Shanghai checkpoint, exactly as the
        # legacy loop did.  ``exchange_date`` remains explicit in the
        # scheduler contract for the other persisted operations.
        _ = exchange_date
        return await build_market_snapshot(MarketSnapshotRequest(session=session, refresh_public_quotes=True))

    async def build_board_report() -> dict[str, Any]:
        return await run_intraday_board_report(deliver=False)

    async def settle_outcomes(exchange_date: date) -> dict[str, Any]:
        return await run_database_blocking(recompute_outcomes, exchange_date, timeout_seconds=60)

    async def settle_analyst_intraday_outcomes(exchange_date: date) -> dict[str, Any]:
        return await run_database_blocking(
            recompute_analyst_intraday_outcomes_for_date, exchange_date, timeout_seconds=90,
        )

    async def settle_scorecards(exchange_date: date) -> dict[str, Any]:
        return await run_database_blocking(recompute_scorecards, exchange_date, timeout_seconds=30)

    async def persist_review(exchange_date: date, session: str) -> None:
        def persist() -> None:
            with db.transaction() as connection:
                strategy_review_payload(
                    connection,
                    StrategyReviewRequest(session=session, as_of_date=exchange_date, persist=True),
                )
        await run_database_blocking(persist, timeout_seconds=30)

    async def review_completed_for_checkpoint(exchange_date: date, session: str) -> bool:
        def load() -> bool:
            with db.transaction() as connection:
                return review_checkpoint_completed_isolated(connection, exchange_date, session)
        return await run_database_blocking(load, timeout_seconds=10)

    async def build_analyst_review(cadence: str, exchange_date: date) -> dict[str, Any]:
        return await run_database_blocking(
            build_recorded_analyst_market_review, db, cadence, exchange_date, timeout_seconds=90,
        )

    await strategy_review_scheduler(StrategyReviewSchedulerDependencies(
        calendar_open=sse_calendar_open_async,
        sync_index_context=sync_strategy_index_context,
        build_market_snapshot=build_snapshot,
        build_board_report=build_board_report,
        recompute_outcomes=settle_outcomes,
        recompute_analyst_intraday_outcomes=settle_analyst_intraday_outcomes,
        recompute_scorecards=settle_scorecards,
        build_analyst_market_review=build_analyst_review,
        persist_review=persist_review,
        completed_for_checkpoint=review_completed_for_checkpoint,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
    ))


async def post_close_strategy_loop() -> None:
    """Compose the independent same-date post-close scheduler.

    Persistence and provider boundaries remain here; the scheduler module owns
    only retry-window and completion semantics, so it can be tested without
    the service database or wall clock.
    """
    async def completed_for_date(exchange_date: date) -> tuple[bool, bool]:
        return (
            await run_database_blocking(post_close_strategy_completed_for_date, exchange_date, timeout_seconds=10),
            await run_database_blocking(watchlist_main_wave_completed_for_date, exchange_date, timeout_seconds=10),
        )

    async def run_strategy(exchange_date: date) -> str:
        result = await run_database_blocking(functools.partial(
            run_recorded, db, task_key="post_close_strategy",
            run_key=f"post-close-strategy:{exchange_date}",
            operation=functools.partial(run_post_close_strategy, PostCloseStrategyRequest(as_of_date=exchange_date)),
            cadence="daily", as_of_date=exchange_date, methodology_version=POST_CLOSE_STRATEGY_MODEL_VERSION,
            input_summary={"data_boundary": "same_date_close"},
        ), timeout_seconds=60)
        return str(result.get("status") or "failed")

    async def run_main_wave(exchange_date: date) -> str:
        result = await run_database_blocking(functools.partial(
            run_recorded, db, task_key="watchlist_main_wave",
            run_key=f"watchlist-main-wave:{exchange_date}",
            operation=functools.partial(persist_watchlist_main_wave_research, WatchlistMainWaveResearchRequest(as_of_date=exchange_date)),
            cadence="daily", as_of_date=exchange_date, methodology_version="watchlist-main-wave-v2",
            input_summary={"universe": "watchlist"},
        ), timeout_seconds=90)
        return str(result.get("status") or "failed")

    await post_close_strategy_scheduler(PostCloseSchedulerDependencies(
        calendar_open=sse_calendar_open_async,
        retry_window=post_close_strategy_retry_window,
        completed_for_date=completed_for_date,
        run_strategy=run_strategy,
        run_main_wave=run_main_wave,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
    ))


def post_close_strategy_completed_for_date(as_of_date: date) -> bool:
    """Check the persisted result for exactly one exchange date and model."""
    return persisted_post_close_strategy_completed_for_date(
        db, as_of_date, model_version=POST_CLOSE_STRATEGY_MODEL_VERSION,
    )


def watchlist_main_wave_completed_for_date(as_of_date: date) -> bool:
    """Return whether both same-date daily watchlist priors were materialized."""
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT count(DISTINCT strategy_key)::int AS completed FROM quant.strategy_experiments
                WHERE strategy_key=ANY(%s) AND universe_key='watchlist'
                  AND end_date=%s AND status='completed'""",
            ([WATCHLIST_MAIN_WAVE_STRATEGY_KEY, WATCHLIST_REBOUND_STRATEGY_KEY], as_of_date),
        ).fetchone()
    return bool(row and int(row["completed"] or 0) == 2)


def build_daily_strategy_summary(exchange_date: date) -> dict[str, Any]:
    """Compatibility wrapper; projection logic lives outside the composition root."""
    return build_daily_strategy_summary_projection(
        db, exchange_date, readiness=feature_readiness_state,
        json_safe=strategy_json_safe, policy_review=contextual_bandit_policy_review,
    )


async def run_daily_strategy_summary(exchange_date: date) -> dict[str, Any]:
    """Persist the daily summary for the frontend without external delivery."""
    summary = await run_database_blocking(build_daily_strategy_summary, exchange_date)
    dashboard_url = (os.getenv("QUANT_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/") or None
    text = daily_strategy_summary_text(summary, dashboard_url)

    def persist_frontend_only() -> None:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.strategy_day_summaries(exchange_date,payload,message_text,delivery_status,error_message)
                   VALUES(%s,%s,%s,'suppressed','suppressed: Feishu is reserved for watched-stock strategy signals')
                   ON CONFLICT(exchange_date) DO UPDATE SET payload=EXCLUDED.payload,message_text=EXCLUDED.message_text,
                       delivery_status='suppressed',next_attempt_at=NULL,
                       error_message=EXCLUDED.error_message,updated_at=now()""",
                (exchange_date, Json(strategy_json_safe(summary)), text),
            )
    await run_database_blocking(persist_frontend_only)
    return {"status": "suppressed", "exchange_date": str(exchange_date), "summary": summary,
            "reason": "Feishu is reserved for watched-stock strategy signals"}

async def daily_strategy_summary_loop() -> None:
    """Deliver one compact review after the post-close candidate retry window."""
    async def terminal_for_date(exchange_date: date) -> bool:
        def load() -> bool:
            with db.transaction() as connection:
                return daily_summary_terminal_isolated(connection, exchange_date)
        return await run_database_blocking(load, timeout_seconds=10)
    await daily_strategy_summary_scheduler(DailyStrategySummarySchedulerDependencies(
        calendar_open=sse_calendar_open_async,
        terminal_for_date=terminal_for_date,
        run_summary=run_daily_strategy_summary,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
    ))


async def intraday_monitor_loop(interval_seconds: int) -> None:
    """Run only during continuous auction with a bounded adaptive cadence."""
    await run_intraday_monitor_loop(
        interval_seconds,
        realtime_session=realtime_market_session_async,
        high_frequency_window=intraday_high_frequency_window,
        next_delay_seconds=intraday_next_monitor_delay_seconds,
        make_scan_request=lambda limit, offset: IntradayScanRequest(
            realtime_validation_limit=limit,
            realtime_validation_offset=offset,
        ),
        scan_watchlist=run_intraday_watchlist_scan,
        board_refresh_interval_seconds=intraday_board_refresh_interval_seconds,
        run_board_report=run_intraday_board_report,
    )


def persist_intraday_super_get_fast_quote(symbol: str, observed_at: datetime, price: float,
                                          pct_change: float | None, row: dict[str, Any],
                                          provider_key: str, latency_ms: int) -> None:
    """Persist one-second quote evidence outside the asyncio event loop."""
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO quant.intraday_quote_observations(
                   scan_id,symbol,observed_at,source_name,price,pct_change,raw
               ) VALUES(null,%s,%s,'tushare_super_get_rt_k',%s,%s,%s)""",
            (symbol, observed_at, price, pct_change, Json(strategy_json_safe(row))),
        )
        record_provider_success(connection, provider_key, "realtime_quote", 1, latency_ms)


def record_intraday_super_get_fast_quote_failure(error: str, latency_ms: int | None = None) -> None:
    with db.transaction() as connection:
        record_provider_failure(connection, "tushare_super_get", "realtime_quote", error, latency_ms)


async def capture_intraday_super_get_fast_quote(symbol: str) -> dict[str, Any]:
    """Persist one lightweight rt_k cross-check without creating fetch-run churn."""
    observed_at = datetime.now(timezone.utc)
    started_at = asyncio.get_running_loop().time()
    try:
        result = await call_tushare_api("rt_k", {"ts_code": symbol}, None, "super_get")
        row = next((item for item in result.rows if str(item.get("ts_code") or "").upper() == symbol),
                   result.rows[0] if result.rows else None)
        if row is None:
            return {"status": "empty", "symbol": symbol, "observed_at": observed_at.isoformat()}
        price = intraday_number(row.get("close"))
        previous_close = intraday_number(row.get("pre_close"))
        if price is None or price <= 0:
            raise ProviderCallError("rt_k returned no valid positive close")
        pct_change = ((price / previous_close) - 1) * 100 if previous_close and previous_close > 0 else None
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database_blocking(
            persist_intraday_super_get_fast_quote, symbol, observed_at, price, pct_change,
            row, result.provider.key, latency_ms,
        )
        return {"status": "completed", "symbol": symbol, "observed_at": observed_at.isoformat(), "price": price}
    except Exception as error:  # noqa: BLE001 - the next one-second slot remains useful
        detail = safe_error_detail(str(error), 300)
        # A circuit-open response means the request was deliberately not sent
        # upstream.  Do not turn local protection into another provider
        # failure, otherwise the five-minute window is extended every second
        # and the route can never recover on its own.
        if isinstance(error, HTTPException) and is_circuit_open_http_error(error):
            return {"status": "circuit_open", "symbol": symbol, "observed_at": observed_at.isoformat(),
                    "error": detail}
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database_blocking(record_intraday_super_get_fast_quote_failure, detail, latency_ms)
        return {"status": "failed", "symbol": symbol, "observed_at": observed_at.isoformat(),
                "error": detail}


async def intraday_super_get_fast_quote_loop() -> None:
    """Run the optional one-second rt_k cross-check in special windows."""
    async def load_symbols() -> list[str]:
        def load_watches() -> list[Any]:
            with db.transaction() as connection:
                return connection.execute(
                    "SELECT * FROM quant.intraday_watchlists WHERE enabled "
                    "ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
                    (intraday_super_get_fast_max_symbols(),),
                ).fetchall()
        rows = await run_database_blocking(load_watches)
        return [str(row["symbol"]) for row in sorted((dict(row) for row in rows), key=intraday_watch_priority_key)]

    async def prune_before(cutoff: datetime) -> None:
        def prune() -> None:
            with db.transaction() as connection:
                connection.execute(
                    "DELETE FROM quant.intraday_quote_observations "
                    "WHERE source_name='tushare_super_get_rt_k' AND observed_at<%s",
                    (cutoff,),
                )
        await run_database_blocking(prune)

    await run_intraday_fast_quote_loop(
        realtime_session=realtime_market_session_async,
        high_frequency_window=intraday_high_frequency_window,
        load_symbols=load_symbols,
        prune_before=prune_before,
        storage_allowed=nonessential_high_frequency_capture_allowed,
        capture_quote=capture_intraday_super_get_fast_quote,
        observe_completed=observe_completed_task,
        interval_seconds=intraday_super_get_fast_interval_seconds,
        max_in_flight=intraday_super_get_fast_max_in_flight,
        retention_days=intraday_fast_quote_retention_days,
    )


async def intraday_minute_profile_capture_loop() -> None:
    """Capture the explicit-watch EAC baseline once near each A-share close.

    Tencent minute tapes are requested during the final continuous-auction
    window. A failed fetch may retry during the short 14:55--14:59 window; a
    completed or partial capture is never repeated that day.
    """
    completed: set[date] = set()
    while True:
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        if (local.date() not in completed and await sse_calendar_open_async(local.date())
                and time(14, 55) <= local.time() < time(15, 0)):
            def load_watches() -> list[Any]:
                with db.transaction() as connection:
                    return connection.execute(
                        "SELECT * FROM quant.intraday_watchlists WHERE enabled ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
                        (intraday_minute_profile_max_symbols(),),
                    ).fetchall()
            rows = await run_database_blocking(load_watches)
            symbols = [str(row["symbol"]) for row in sorted((dict(row) for row in rows), key=intraday_watch_priority_key)]
            if symbols:
                allowed, storage = await nonessential_high_frequency_capture_allowed()
                if not allowed:
                    print(f"intraday minute-profile capture skipped by storage guard: {storage.get('state')}")
                    completed.add(local.date())
                    await asyncio.sleep(30)
                    continue
                try:
                    result = await capture_intraday_minute_sessions(symbols)
                    if result["status"] in {"completed", "partial"}:
                        completed.add(local.date())
                    elif result["status"] == "blocked":
                        completed.add(local.date())
                except Exception as error:  # noqa: BLE001 - retry while close window remains open
                    print(f"intraday minute profile capture failed: {str(error)[:300]}")
        await asyncio.sleep(30)


def intraday_flow_label(value: Any) -> str:
    number_value = intraday_number(value)
    if number_value is None:
        return "—"
    absolute = abs(number_value)
    if absolute >= 100_000_000:
        return f"{number_value / 100_000_000:+.2f}亿"
    if absolute >= 10_000:
        return f"{number_value / 10_000:+.1f}万"
    return f"{number_value:+.2f}"


async def run_intraday_board_report(*, deliver: bool = False) -> dict[str, Any]:
    """Persist an evidence-labelled sector/mining brief for the frontend.

    ``deliver`` remains only for compatible callers; board and linkage mining
    never publish to Feishu under the watched-stock-only policy.
    """
    observed_at = datetime.now(timezone.utc)
    # Persist the full bounded Top10 requested by the close-review surface.
    # ``quoted_members`` remains visible so sparse public quote coverage is not
    # presented as complete membership coverage.
    report = await intraday_sector_report(IntradaySectorReportRequest(kind="all", top_stocks=10, hydrate_top_boards=0))
    report_id = uuid.uuid4()
    if report.get("status") != "completed":
        def persist_blocked() -> None:
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO quant.intraday_board_reports(board_report_id,observed_at,status,source_status,summary,payload)
                       VALUES(%s,%s,'blocked',%s,%s,%s)""",
                    (report_id, observed_at, Json(strategy_json_safe(report.get("sources", {}))),
                     Json({"reason": report.get("reason")}), Json(strategy_json_safe(report))),
                )
        await run_database_blocking(persist_blocked)
        return {"status": "blocked", "board_report_id": str(report_id), "reason": report.get("reason")}
    runtime_quotes = report.pop("_runtime_quotes", {})
    mining_candidates, mining_coverage, mining_summary = board_stock_mining_candidates(report["items"])
    # Full member quotes are runtime evidence for the miner only.  The board
    # report remains bounded to Top10 so routine five-minute storage does not
    # grow with every mapped constituent.
    stored_report = {
        **report,
        "items": [{key: value for key, value in item.items() if key != "member_quotes"} for item in report["items"]],
    }
    sections: list[str] = []
    summary: dict[str, Any] = {}
    for taxonomy_key, label in (("eastmoney_industry", "行业"), ("eastmoney_concept", "概念")):
        rows = [item for item in report["items"] if item.get("taxonomy_key") == taxonomy_key and item.get("net_inflow") is not None]
        inflow = sorted(rows, key=lambda item: float(item["net_inflow"]), reverse=True)[:3]
        outflow = sorted(rows, key=lambda item: float(item["net_inflow"]))[:3]
        summary[taxonomy_key] = {"inflow": inflow, "outflow": outflow}
        def render(items: list[dict[str, Any]]) -> str:
            return "；".join(f"{item['label']} {intraday_flow_label(item['net_inflow'])}" for item in items) or "—"
        sections.extend([f"{label}流入：{render(inflow)}", f"{label}流出：{render(outflow)}"])
    def persist_completed() -> None:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.intraday_board_reports(board_report_id,observed_at,status,source_status,summary,payload)
                   VALUES(%s,%s,'completed',%s,%s,%s)""",
                (report_id, observed_at,
                 Json(strategy_json_safe({"coverage": report.get("coverage"), "tushare_context": report.get("tushare_context")})),
                 Json(strategy_json_safe(summary)), Json(strategy_json_safe(stored_report))),
            )
    await run_database_blocking(persist_completed)
    mining = {"status": "completed", "summary": mining_summary, "coverage": mining_coverage}
    try:
        def persist_mining() -> str:
            with db.transaction() as connection:
                return persist_board_stock_mining_run(
                    connection, board_report_id=report_id, observed_at=observed_at,
                    candidates=mining_candidates, coverage=mining_coverage, summary=mining_summary,
                )
        mining["mining_run_id"] = await run_database_blocking(persist_mining)
    except Exception as error:  # The durable board report must survive a mining storage fault.
        print(f"intraday board stock mining persistence failed: {safe_error_detail(str(error), 300)}")
        mining = {"status": "partial", "reason": safe_error_detail(str(error), 300),
                  "summary": mining_summary, "coverage": mining_coverage}
    limit_anchor_refresh = await refresh_intraday_limit_up_anchors(observed_at)
    linkage = await run_limit_linkage_mining(observed_at, runtime_quotes)
    # Keep the board brief and linkage candidates in persistent frontend
    # evidence, never an outbound notification stream.
    linkage_candidates = linkage.get("candidates") or []
    if linkage.get("status") == "completed" and linkage_candidates:
        rendered = "；".join(
            f"{item.get('name') or item['symbol']} {item['symbol']}（{intraday_number(item.get('pct_change')) or 0.0:+.2f}% / 量比{intraday_number(item.get('volume_ratio')) or 0.0:.2f} / 分{intraday_number(item.get('score')) or 0.0:.0f}）"
            for item in linkage_candidates[:20]
        )
        sections.append(f"涨停关联候选（Top{min(20, len(linkage_candidates))}）：{rendered}")
    elif linkage.get("status") == "completed":
        sections.append("涨停关联候选：本轮无满足严格量价门槛的非涨停标的")
    local_time = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    text = "\n".join([
        f"【盘中板块与关联挖掘快报｜{local_time}】", *sections,
        "来源：东财实时板块资金流、东财涨停池、同花顺精确概念成员、腾讯全 A 行情；关联候选仅供研究，须经分钟承接确认，不构成买卖指令。",
    ])
    delivery = {"status": "suppressed", "reason": "Feishu is reserved for watched-stock strategy signals"}
    return {"status": "completed", "board_report_id": str(report_id), "summary": summary,
            "mining": mining, "limit_anchor_refresh": limit_anchor_refresh,
            "limit_linkage_mining": linkage, "delivery": delivery}


async def refresh_intraday_limit_up_anchors(observed_at: datetime) -> dict[str, Any]:
    """Refresh one factual live limit-up pool before linkage mining.

    Tushare limit-list endpoints remain the preferred second source when they
    return same-date rows.  Today they have a valid empty response, therefore
    this bounded Eastmoney fact pool is the live anchor rather than a stale
    close-only Tushare result.
    """
    trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        rows = await run_akshare_blocking(akshare_live_limit_up_pool_events, trade_date, timeout_seconds=20)
        stored = await run_database_blocking(
            persist_akshare_probe_result, "limit_pool", rows, "", timeout_seconds=30,
        )
        return {"status": "completed" if rows else "empty", "received": len(rows), "stored": stored,
                "source": "eastmoney_limit_up_pool"}
    except ExecutorSaturatedError as error:
        return {"status": "blocked", "reason": safe_error_detail(str(error), 300)}
    except (asyncio.TimeoutError, AkShareProviderError, ValueError) as error:
        await run_database_blocking(persist_akshare_probe_failure, "limit_pool", str(error) or "limit-up pool request failed")
        return {"status": "failed", "reason": safe_error_detail(str(error), 300)}


def limit_linkage_relations_from_database(trade_date: date) -> list[dict[str, Any]]:
    """Return exact THS-concept peers of same-date Eastmoney limit-up facts."""
    with db.transaction() as connection:
        rows = connection.execute(
            """WITH anchors AS (
                   SELECT DISTINCT event.symbol,coalesce(instrument.name,event.symbol) AS name
                     FROM quant.market_events event
                LEFT JOIN quant.instruments instrument ON instrument.symbol=event.symbol
                    WHERE event.event_type='limit_up_pool'
                      AND (event.occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                 ), eligible_concepts AS (
                   SELECT sector_key
                     FROM quant.sector_membership_history
                    WHERE taxonomy_key='ths_concept_flow' AND effective_to IS NULL
                    GROUP BY sector_key
                   HAVING count(*) BETWEEN 2 AND 200
                 ), shared AS (
                   SELECT candidate.symbol,anchor.symbol AS leader_symbol,anchor.name AS leader_name,
                          leader.sector_key,sector.label
                     FROM anchors anchor
                     JOIN quant.sector_membership_history leader
                       ON leader.symbol=anchor.symbol AND leader.taxonomy_key='ths_concept_flow' AND leader.effective_to IS NULL
                     JOIN eligible_concepts eligible ON eligible.sector_key=leader.sector_key
                     JOIN quant.sector_membership_history candidate
                       ON candidate.taxonomy_key=leader.taxonomy_key AND candidate.sector_key=leader.sector_key
                      AND candidate.effective_to IS NULL AND candidate.symbol<>anchor.symbol
                      AND candidate.symbol NOT IN (SELECT symbol FROM anchors)
                     JOIN quant.sectors sector ON sector.taxonomy_key=leader.taxonomy_key AND sector.sector_key=leader.sector_key
                 )
                 SELECT symbol,array_agg(DISTINCT sector_key) AS concept_keys,array_agg(DISTINCT label) AS concept_labels,
                        array_agg(DISTINCT leader_symbol) AS leader_symbols,array_agg(DISTINCT leader_name) AS leader_names
                   FROM shared GROUP BY symbol""",
            (trade_date,),
        ).fetchall()
    return [{**dict(row), "shared_concepts": len(row["concept_keys"] or [])} for row in rows]


async def run_limit_linkage_mining(observed_at: datetime, quote_by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Mine non-limit peers without another all-market quote request."""
    trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        relations = await run_database_blocking(limit_linkage_relations_from_database, trade_date)
    except Exception as error:
        # A research-only miner must never make the durable board-report path
        # fail.  Its fault is persisted in normal logs and surfaced locally.
        return {"status": "failed", "reason": safe_error_detail(str(error), 300), "summary": {"candidate_count": 0}}
    candidates, summary = limit_linkage_candidates(relations, quote_by_symbol)
    if not relations:
        return {"status": "blocked", "reason": "no same-date Eastmoney limit-up anchors with exact THS concept membership", "summary": summary}
    try:
        def persist() -> str:
            with db.transaction() as connection:
                return persist_limit_linkage_mining_run(
                    connection, observed_at=observed_at, trade_date=trade_date, candidates=candidates, summary=summary,
                )
        run_id = await run_database_blocking(persist)
    except Exception as error:
        return {"status": "partial", "reason": safe_error_detail(str(error), 300), "summary": summary}
    return {"status": "completed", "linkage_run_id": run_id, "summary": summary, "candidates": candidates}


STRATEGY_DECISION_MODEL_VERSION = "intraday-multisource-v1"


def strategy_json_safe(value: Any) -> Any:
    """Normalize database rows (dates/timestamps included) for JSON evidence."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# Compatibility names remain in the composition root for existing routers and
# tests, while the live implementation is now the I/O-free market_regimes
# module shared by review and future replay.
strategy_rank = pure_strategy_rank
strategy_market_regime = pure_strategy_market_regime
strategy_market_state = pure_strategy_market_state
strategy_index_regime = pure_strategy_index_regime


async def sync_strategy_index_context(as_of_date: date) -> dict[str, Any]:
    """Persist bounded close-daily index context through the non-realtime primary route."""
    start_date = as_of_date - timedelta(days=45)
    requests = [TushareFetchRequest(
        api_name="index_daily", provider="primary",
        params={"ts_code": symbol, "start_date": start_date.strftime("%Y%m%d"),
                "end_date": as_of_date.strftime("%Y%m%d")}, max_rows=60, force_refresh=True,
    ) for symbol in STRATEGY_INDEX_SYMBOLS]
    results = await asyncio.gather(*(fetch_tushare_catalog(request) for request in requests), return_exceptions=True)
    completed, errors = [], {}
    for symbol, result in zip(STRATEGY_INDEX_SYMBOLS, results, strict=True):
        if isinstance(result, Exception):
            errors[symbol] = str(result)[:240]
        else:
            completed.append(symbol)
    return {"status": "completed" if not errors else "partial", "completed": completed, "errors": errors,
            "source": "tushare_primary index_daily; close-daily context only"}


def analyst_execution_context(connection: Any, as_of_date: date, observed_at: datetime | None = None) -> dict[str, Any]:
    """Expose analyst text as a gated prior rather than a trade instruction."""
    summary = analyst_text_factor_summary(connection, as_of_date, available_before=observed_at)
    promotion = analyst_live_promotion(connection, as_of_date)
    return {"factor_version": summary["factor_version"], "market": summary["market"], "themes": summary["themes"],
            "mature_analysts": [], "eligible_themes": [],
            "scorecard_readiness": analyst_scorecard_readiness(connection),
            "execution_eligible": promotion["execution_eligible"], "max_live_weight": promotion["weight"],
            "role": "small_prior" if promotion["execution_eligible"] else "research_context_only",
            "reason": promotion["reason"], "promotion": promotion,
            "data_boundary": summary["data_boundary"]}


def strategy_index_breadth_context(connection: Any, as_of_date: date, session: str, observed_at: datetime) -> dict[str, Any]:
    """Return only index/breadth evidence available at the review checkpoint."""
    snapshot = connection.execute(
        """SELECT observed_at,status,coverage,summary,quality_flags,source_summary
             FROM quant.market_snapshot_runs
             WHERE exchange_date=%s AND session=%s AND observed_at<=%s
             ORDER BY observed_at DESC LIMIT 1""",
        (as_of_date, session, observed_at),
    ).fetchone()
    index = connection.execute(
        """SELECT trading_date,close,pre_close,available_at FROM quant.canonical_bars_daily
             WHERE symbol='000300.SH' AND trading_date<=%s AND available_at<=%s
             ORDER BY trading_date DESC LIMIT 1""",
        (as_of_date, observed_at),
    ).fetchone()
    index_rows = connection.execute(
        """WITH ranked AS (
               SELECT symbol,trading_date,open,high,low,close,volume,available_at,
                      row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS recent_rank
                 FROM quant.market_bars_daily
                WHERE symbol=ANY(%s) AND trading_date<=%s AND available_at<=%s
           )
           SELECT symbol,trading_date,open,high,low,close,volume,available_at
             FROM ranked WHERE recent_rank<=30 ORDER BY symbol,trading_date DESC""",
        (list(STRATEGY_INDEX_SYMBOLS), as_of_date, observed_at),
    ).fetchall()
    context: dict[str, Any] = {"index": None, "multi_index_regime": strategy_index_regime([dict(row) for row in index_rows]),
                               "breadth": None, "quality_flags": []}
    latest_index_dates = {item["symbol"]: item["trading_date"] for item in context["multi_index_regime"]["items"]}
    if any(value != str(as_of_date) for value in latest_index_dates.values()) or len(latest_index_dates) < 3:
        context["quality_flags"].append("multi_index_close_context_not_current")
    if index:
        close, pre_close = number(index["close"]), number(index["pre_close"])
        context["index"] = {"symbol": "000300.SH", "trading_date": str(index["trading_date"]), "close": close,
                            "change_pct": round((close / pre_close - 1) * 100, 4) if pre_close else None,
                            "available_at": index["available_at"].isoformat(), "role": "daily close context, not intraday index quote"}
        if index["trading_date"] != as_of_date:
            context["quality_flags"].append("index_not_current_exchange_date")
    else:
        context["quality_flags"].append("missing_index_context")
    if snapshot and int((snapshot["summary"] or {}).get("priced_symbols") or 0) > 0:
        summary = dict(snapshot["summary"] or {})
        advancing, declining = int(summary.get("advancers") or 0), int(summary.get("decliners") or 0)
        known = advancing + declining
        advance_share = advancing / known if known else None
        breadth_state = "broad_positive" if advance_share is not None and advance_share >= 0.60 else \
                        "broad_negative" if advance_share is not None and advance_share <= 0.40 else "mixed"
        context["breadth"] = {"observed_at": snapshot["observed_at"].isoformat(), "status": snapshot["status"],
                              "coverage": number(snapshot["coverage"]), "advancers": advancing, "decliners": declining,
                              "unchanged": int(summary.get("unchanged") or 0), "advance_share": round(advance_share, 4) if advance_share is not None else None,
                              "median_change_pct": summary.get("median_change_pct"), "state": breadth_state}
        context["quality_flags"].extend(list(snapshot["quality_flags"] or []))
    else:
        context["quality_flags"].append("missing_usable_breadth_snapshot")
    return context


def strategy_review_payload(connection: Any, request: StrategyReviewRequest) -> dict[str, Any]:
    """Compatibility wrapper for the isolated persisted review projection."""
    return build_strategy_review_isolated(
        connection,
        request,
        market_state=strategy_market_state,
        index_breadth_context=strategy_index_breadth_context,
        analyst_context=analyst_execution_context,
        json_safe=strategy_json_safe,
    )

def intraday_decision_card(connection: Any, symbol: str) -> dict[str, Any]:
    """Compatibility facade for the isolated local-only decision-card projection."""
    return read_intraday_decision_card(
        connection, symbol,
        strategy_market_state_fn=strategy_market_state,
        analyst_execution_context_fn=analyst_execution_context,
        json_safe_fn=strategy_json_safe,
    )


def strategy_intraday_candidates(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Turn an exact board-member join into transparent, bounded candidates.

    Board scores use within-taxonomy ranks.  Stock scores then use Tencent's
    relative main flow, volume ratio, turnover and price change.  The function
    is deliberately pure so its time semantics and risk gates are testable.
    """
    by_taxonomy: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if int(item.get("mapped_members") or 0) > 0 and item.get("top_stocks"):
            by_taxonomy.setdefault(str(item.get("taxonomy_key")), []).append(item)

    board_scores: dict[tuple[str, str], float] = {}
    for taxonomy_key, boards in by_taxonomy.items():
        flow_ranks = strategy_rank([intraday_number(item.get("net_inflow")) for item in boards])
        change_ranks = strategy_rank([intraday_number(item.get("change_pct")) for item in boards])
        for index, board in enumerate(boards):
            flow = intraday_number(board.get("net_inflow"))
            board_scores[(taxonomy_key, str(board.get("sector_key")))] = round(
                35 * flow_ranks.get(index, 0.0) + 15 * change_ranks.get(index, 0.0) + (5 if (flow or 0) > 0 else 0), 2
            )

    proposals: dict[str, dict[str, Any]] = {}
    for taxonomy_key, boards in by_taxonomy.items():
        for board in boards:
            board_key = (taxonomy_key, str(board.get("sector_key")))
            for stock in board.get("top_stocks") or []:
                symbol = str(stock.get("symbol") or "")
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                    continue
                proposed = {
                    "symbol": symbol, "name": stock.get("name"), "taxonomy_key": taxonomy_key,
                    "sector_key": board_key[1], "sector_label": board.get("label"),
                    "board_score": board_scores.get(board_key, 0.0), "board_net_inflow": intraday_number(board.get("net_inflow")),
                    "board_change_pct": intraday_number(board.get("change_pct")), "main_net_inflow": intraday_number(stock.get("main_net_inflow")),
                    "volume_ratio": intraday_number(stock.get("volume_ratio")), "turnover_rate": intraday_number(stock.get("turnover_rate")),
                    "pct_change": intraday_number(stock.get("pct_change")), "turnover": intraday_number(stock.get("turnover")),
                }
                existing = proposals.get(symbol)
                if existing is None or proposed["board_score"] > existing["board_score"]:
                    proposals[symbol] = proposed

    candidates = list(proposals.values())
    flow_ranks = strategy_rank([candidate["main_net_inflow"] for candidate in candidates])
    for index, candidate in enumerate(candidates):
        volume_ratio, turnover_rate, pct_change = candidate["volume_ratio"], candidate["turnover_rate"], candidate["pct_change"]
        volume_score = min(max(volume_ratio or 0, 0), 6) / 6 * 15
        turnover_score = min(max(turnover_rate or 0, 0), 20) / 20 * 10
        price_score = min(max(pct_change or 0, 0), 8) / 8 * 5
        score = candidate["board_score"] + 20 * flow_ranks.get(index, 0.0) + volume_score + turnover_score + price_score
        flags = ["public_intraday_sources_only"]
        if (candidate["main_net_inflow"] or 0) <= 0:
            flags.append("nonpositive_main_net_inflow")
        if (candidate["board_net_inflow"] or 0) <= 0:
            flags.append("nonpositive_board_net_inflow")
        if (pct_change or 0) >= 8:
            flags.append("price_extension")
        if (turnover_rate or 0) >= 25:
            flags.append("very_high_turnover")
        hard_no_trade = {"nonpositive_main_net_inflow", "nonpositive_board_net_inflow"}.intersection(flags)
        if hard_no_trade:
            decision = "no_trade"
        elif score >= 70 and "price_extension" not in flags:
            decision = "research_candidate"
        else:
            decision = "watch"
        candidate.update({"score": round(max(0.0, min(score, 100.0)), 2), "decision": decision,
                          "confidence": round(min(0.7, 0.25 + score / 200), 3), "risk_flags": flags})
    candidates.sort(key=lambda item: (item["decision"] != "research_candidate", -item["score"], item["symbol"]))
    return candidates[:limit]


def strategy_event_context(symbols: list[str], observed_at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read only evidence that was available by the snapshot time.

    龙虎榜/涨停池 events are returned as next-session context, never as a
    same-day intraday score component.
    """
    if not symbols:
        return {}
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT symbol,event_type,title,available_at
                 FROM quant.market_events
                WHERE symbol=ANY(%s) AND available_at<=%s
                  AND event_type=ANY(%s)
                ORDER BY available_at DESC LIMIT 100""",
            (symbols, observed_at, ["lhb_event", "strong_pool", "limit_up_pool", "previous_limit_pool", "limit_open_pool"]),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(dict(row))
    return grouped


def strategy_tushare_lhb_context(symbols: list[str], observed_at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read Tushare龙虎榜 evidence already available at the snapshot time.

    `top_list`/`top_inst` are post-close facts.  They deliberately remain
    explanation-only and cannot influence a same-day intraday rank.
    """
    if not symbols:
        return {}
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT api_name,row_data,available_at
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('top_list','top_inst') AND available_at<=%s
                  AND row_data->>'ts_code'=ANY(%s)
                ORDER BY available_at DESC,record_index LIMIT 100""",
            (observed_at, symbols),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row["row_data"])
        symbol = str(payload.get("ts_code") or "")
        if symbol:
            grouped.setdefault(symbol, []).append({"api_name": row["api_name"], "available_at": row["available_at"], "row": payload})
    return grouped


def strategy_source_readiness(observed_at: datetime) -> dict[str, Any]:
    """Expose source freshness and ownership without inventing source parity."""
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT provider_key,capability,last_success_at,last_failure_at,last_row_count,consecutive_failures
                 FROM quant.provider_health
                WHERE provider_key IN ('akshare','eastmoney_free','tencent_free','tushare_primary','tushare_super_sdk','tushare_super_get')
                ORDER BY provider_key,capability"""
        ).fetchall()
        event_rows = connection.execute(
            """SELECT source,event_type,max(available_at) latest_available_at,count(*)::int rows
                 FROM quant.market_events WHERE available_at<=%s
                GROUP BY source,event_type ORDER BY source,event_type""",
            (observed_at,),
        ).fetchall()
    providers: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = providers.setdefault(str(row["provider_key"]), {"capabilities": []})
        provider["capabilities"].append(strategy_json_safe(dict(row)))
    return {
        "providers": providers,
        "post_close_event_inventory": strategy_json_safe([dict(row) for row in event_rows]),
        "xinhua_finance": {
            "status": "configured_contract_required" if any(item.get("provider_key") == "xinhua_finance" and item.get("configured") for item in free_provider_status()) else "not_configured",
            "reason": "requires the licensed API URL, authentication scheme and response-field contract; no public endpoint is guessed",
        },
    }


async def strategy_tushare_realtime_validation(symbols: list[str], enabled: bool) -> dict[str, Any]:
    """Validate at most three candidates through the verified super GET path."""
    if not enabled or not symbols:
        return {"status": "skipped", "reason": "disabled or no candidates", "items": []}
    active, reason = await realtime_market_session_async("rt_k")
    if not active:
        return {"status": "skipped", "reason": reason, "items": []}
    results: list[dict[str, Any]] = []
    for symbol in symbols[:3]:
        source, rows = await stock_study_fetch(
            "tushare_rt_k",
            TushareFetchRequest(api_name="rt_k", provider="super", params={"ts_code": symbol}, max_rows=1, force_refresh=True),
        )
        latest = rows[0] if rows else {}
        results.append({"symbol": symbol, "source": source, "latest": latest})
    status = "completed" if any(item["source"]["status"] in {"completed", "partial", "unchanged"} for item in results) else "failed"
    return {"status": status, "items": results}


async def run_strategy_decision(request: StrategyDecisionRequest) -> dict[str, Any]:
    """Compatibility wrapper for the isolated evidence-only decision service."""
    return await run_strategy_decision_isolated(
        request,
        db=db,
        run_database_blocking=run_database_blocking,
        build_intraday_report=intraday_sector_report,
        market_regime=strategy_market_regime,
        select_candidates=strategy_intraday_candidates,
        event_context=strategy_event_context,
        tushare_lhb_context=strategy_tushare_lhb_context,
        source_readiness=strategy_source_readiness,
        tushare_realtime_validation=strategy_tushare_realtime_validation,
        exchange_for=exchange_for,
        json_safe=strategy_json_safe,
        model_version=STRATEGY_DECISION_MODEL_VERSION,
    )

async def sync_ths_industry_moneyflow_legacy(request: SectorFlowSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated THS flow synchronizer."""
    return await sync_ths_industry_moneyflow(request)

async def sync_ths_industry_moneyflow(request: SectorFlowSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated THS industry flow sync."""
    return await sync_ths_industry_isolated(
        request, trade_date=cn_today, fetch_catalog=fetch_tushare_catalog, fetch_request=TushareFetchRequest,
        load_rows=lambda request_key: run_database_blocking(tushare_rows_for_request, request_key),
        run_database_blocking=run_database_blocking, db=db, upsert_taxonomy=upsert_sector_taxonomy, upsert_sector=upsert_sector,
        decimal_or_none=decimal_or_none, json_value=Json, observed_at=lambda: datetime.now(timezone.utc),
    )


async def sync_ths_concept_signals_legacy(request: SectorFlowSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated THS concept synchronizer."""
    return await sync_ths_concept_signals(request)


async def sync_ths_concept_signals(request: SectorFlowSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated THS concept flow sync."""
    return await sync_ths_concept_signals_isolated(
        request, trade_date=cn_today, fetch_catalog=fetch_tushare_catalog, fetch_request=TushareFetchRequest,
        load_rows=lambda request_key: run_database_blocking(tushare_rows_for_request, request_key),
        run_database_blocking=run_database_blocking, db=db, upsert_taxonomy=upsert_sector_taxonomy, upsert_sector=upsert_sector,
        decimal_or_none=decimal_or_none, json_value=Json, observed_at=lambda: datetime.now(timezone.utc), http_exception=HTTPException,
    )


async def sync_ths_concept_members_legacy(request: ConceptMemberSyncRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated THS member synchronizer."""
    return await sync_ths_concept_members(request)

async def sync_ths_concept_members(request: ConceptMemberSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated concept-member sync."""
    return await sync_ths_concept_members_isolated(
        request,
        sync_flow_catalog=sync_ths_concept_signals,
        flow_request=SectorFlowSyncRequest,
        run_database_blocking=run_database_blocking,
        db=db,
        fetch_catalog=fetch_tushare_catalog,
        catalog_request=TushareFetchRequest,
        load_rows=lambda request_key: run_database_blocking(tushare_rows_for_request, request_key),
        persist_members=persist_ths_sector_members,
        observed_at=lambda: datetime.now(timezone.utc),
        http_exception=HTTPException,
    )


def ths_concept_member_backfill_enabled() -> bool:
    return os.getenv("THS_CONCEPT_MEMBER_BACKFILL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def ths_concept_member_backfill_batch_size() -> int:
    try:
        value = int(os.getenv("THS_CONCEPT_MEMBER_BACKFILL_BATCH_SIZE", "25"))
    except ValueError:
        value = 25
    return min(25, max(1, value))


def all_board_member_backfill_enabled() -> bool:
    return os.getenv("ALL_BOARD_MEMBER_BACKFILL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def all_board_member_backfill_batch_size() -> int:
    try:
        value = int(os.getenv("ALL_BOARD_MEMBER_BACKFILL_BATCH_SIZE", "10"))
    except ValueError:
        value = 10
    return min(25, max(1, value))


async def run_all_board_member_backfill_batch(request: AllBoardMemberBackfillRequest) -> dict[str, Any]:
    """Advance exact member coverage without name matching or bulk fan-out.

    Each invocation has a strict batch budget.  The post-close loop repeatedly
    calls this operation, and all sources independently retain their latest
    completed constituent snapshot while transient upstream failures are
    recorded for bounded retry.
    """
    results: list[dict[str, Any]] = []
    if request.refresh_catalogs and request.include_ths:
        results.append({"source": "ths_catalogs", **await sync_all_ths_sector_catalogs()})
    if request.include_ths:
        for index_type in ("N", "I", "R", "S", "ST", "BB"):
            try:
                item = await sync_ths_sector_catalog(SectorCatalogSyncRequest(
                    index_type=index_type, sync_members=True, member_limit=request.batch_size, resume=True,
                ))
                results.append({"source": "ths_member", **item})
            except HTTPException as error:
                results.append({"source": "ths_member", "index_type": index_type, "status": "failed", "reason": str(error.detail)[:300]})
    if request.include_eastmoney:
        for kind in ("industry", "concept"):
            item = await sync_eastmoney_board_members(EastmoneyBoardMemberSyncRequest(
                kind=kind, member_limit=request.batch_size, resume=True,
            ))
            results.append({"source": "eastmoney_member", **item})
    successful = [item for item in results if item.get("status") in {"completed", "partial"}]
    failed = [item for item in results if item.get("status") in {"blocked", "failed"}]
    return {
        "status": "partial" if failed else "completed",
        "batch_size": request.batch_size,
        "results": results,
        "notice": "本次只推进受限批次；自动任务在盘后续跑，成员关系仅来自各源的精确代码/原始成员接口。",
        "successful_sources": len(successful),
        "failed_sources": len(failed),
    }


async def all_board_member_backfill_loop() -> None:
    """Use the quieter post-close window for durable all-board coverage."""
    while True:
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        if await sse_calendar_open_async(local.date()) and time(15, 10) <= local.time() < time(18, 0):
            try:
                await run_all_board_member_backfill_batch(AllBoardMemberBackfillRequest(
                    batch_size=all_board_member_backfill_batch_size(), include_ths=True, include_eastmoney=True,
                ))
            except Exception as error:  # Durable per-board states make the next batch safe.
                print(f"all board member backfill batch failed: {safe_error_detail(str(error), 300)}")
            await asyncio.sleep(90)
            continue
        await asyncio.sleep(60)


async def run_ths_concept_member_backfill_batch(request: ConceptMemberBackfillRequest) -> dict[str, Any]:
    """Refresh daily THS flow once, then resume exact member hydration."""
    trade_date = request.trade_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    def load_existing() -> Any:
        with db.transaction() as connection:
            return connection.execute(
                "SELECT count(*)::int rows FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s",
                (trade_date,),
            ).fetchone()

    existing = await run_database_blocking(load_existing)
    refreshed: dict[str, Any] | None = None
    if request.refresh_flow_catalog or not int(existing["rows"]):
        refreshed = await sync_ths_concept_signals(SectorFlowSyncRequest(trade_date=trade_date, provider=request.provider))
        flow_status = (refreshed.get("sources", {}).get("concept_flow", {}) or {}).get("status")
        if flow_status not in {"completed", "partial", "unchanged", "empty"}:
            return {"status": "blocked", "trade_date": str(trade_date), "refresh": refreshed,
                    "reason": "THS concept flow is unavailable; member mapping was not guessed"}
    result = await sync_ths_concept_members(ConceptMemberSyncRequest(
        trade_date=trade_date, provider=request.provider, member_limit=request.batch_size, resume=True,
    ))
    if result.get("status") == "blocked":
        return {**result, "trade_date": str(trade_date), "refresh": refreshed,
                "progress": {"completed_or_empty": 0, "failed": 0, "remaining": None}}
    def load_progress() -> Any:
        with db.transaction() as connection:
            return connection.execute(
                """SELECT count(*) FILTER (WHERE state IN ('completed','empty'))::int done,
                          count(*) FILTER (WHERE state='failed')::int failed
                     FROM quant.sector_member_sync_state
                    WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s""",
                (trade_date,),
            ).fetchone()

    progress = await run_database_blocking(load_progress)
    return {**result, "refresh": refreshed,
            "progress": {"completed_or_empty": int(progress["done"]), "failed": int(progress["failed"]),
                         "remaining": max(0, int(result["total_concepts"]) - int(progress["done"]))}}


async def ths_concept_member_backfill_loop() -> None:
    """After close, complete one rate-bounded THS member batch at a time."""
    while True:
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        if await sse_calendar_open_async(local.date()) and time(15, 10) <= local.time() < time(18, 0):
            try:
                await run_ths_concept_member_backfill_batch(ConceptMemberBackfillRequest(batch_size=ths_concept_member_backfill_batch_size()))
            except Exception as error:  # noqa: BLE001 - durable state makes the next batch safe to retry
                print(f"THS concept member backfill batch failed: {str(error)[:300]}")
            await asyncio.sleep(65)
            continue
        await asyncio.sleep(60)


async def sync_concept_limit_candidates(request: ConceptCandidateSyncRequest) -> dict[str, Any]:
    """Match high-flow concepts to same-day THS limit-up constituents.

    The two upstream datasets deliberately stay separate.  A candidate exists
    only when a `ths_member` constituent code exactly matches a `limit_list_ths`
    stock code for the same date; text descriptions are not used for matching.
    """
    if request.provider == "super_get":
        return {"status": "blocked", "reason": "complete ths_member snapshots require provider=super, super_sdk, or auto"}
    trade_date = request.trade_date
    def select_concepts() -> tuple[date | None, list[Any]]:
        with db.transaction() as connection:
            selected_date = trade_date or connection.execute(
                "SELECT max(trading_date) latest FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow'"
            ).fetchone()["latest"]
            if selected_date is None:
                return None, []
            concepts = connection.execute(
                """SELECT o.sector_key,s.label,o.net_amount
                     FROM quant.sector_market_observations o
                     JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                    WHERE o.taxonomy_key='ths_concept_flow' AND o.trading_date=%s AND o.net_amount IS NOT NULL
                    ORDER BY o.net_amount DESC,s.label LIMIT %s""",
                (selected_date, request.top_concepts),
            ).fetchall()
        return selected_date, concepts

    selected_date, concepts = await run_database_blocking(select_concepts)
    if selected_date is None:
        return {"status": "blocked", "reason": "sync concept flow before building limit-up candidates"}
    if not concepts:
        return {"status": "blocked", "trade_date": str(selected_date), "reason": "no positive concept-flow cross-section available"}

    observed_at = datetime.now(timezone.utc)
    member_results: list[dict[str, Any]] = []
    concept_keys = [str(item["sector_key"]) for item in concepts]
    for concept in concepts:
        sector_key = str(concept["sector_key"])
        try:
            outcome = await fetch_tushare_catalog(TushareFetchRequest(
                api_name="ths_member", provider=request.provider, params={"ts_code": sector_key}, max_rows=10_000,
                paginate=True, page_size=1000, max_pages=10, require_complete=True,
            ))
            rows = await run_database_blocking(tushare_rows_for_request, str(outcome["request_key"]))
            member_provider = str(outcome["provider"])
            def persist_members() -> int:
                with db.transaction() as connection:
                    return persist_ths_sector_members(connection, "ths_concept_flow", sector_key, rows, member_provider, observed_at)

            stored = await run_database_blocking(persist_members)
            member_results.append({"sector_key": sector_key, "label": concept["label"], "status": outcome["status"],
                                   "members": stored, "provider": outcome["provider"]})
        except HTTPException as error:
            member_results.append({"sector_key": sector_key, "label": concept["label"], "status": "failed",
                                   "members": 0, "error": str(error.detail)})

    stamp = selected_date.strftime("%Y%m%d")
    try:
        limit_outcome = await fetch_tushare_catalog(TushareFetchRequest(
            api_name="limit_list_ths", provider=request.provider, params={"trade_date": stamp}, max_rows=3000,
        ))
    except HTTPException as error:
        return {"status": "partial", "trade_date": str(selected_date), "member_results": member_results,
                "reason": f"limit_list_ths failed: {error.detail}"}
    limit_provider = str(limit_outcome["provider"])
    limit_rows = await run_database_blocking(tushare_rows_for_request, str(limit_outcome["request_key"]))
    limit_by_symbol = {
        str(row.get("ts_code") or "").upper(): row
        for row in limit_rows
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("ts_code") or "").upper()) and row.get("limit_type") == "涨停池"
    }
    membership_status = {str(item["sector_key"]): str(item["status"]) for item in member_results}

    def persist_candidates() -> tuple[int, list[dict[str, Any]]]:
        with db.transaction() as connection:
            memberships = connection.execute(
                """SELECT sector_key,symbol,raw FROM quant.sector_membership_history
                     WHERE taxonomy_key='ths_concept_flow' AND sector_key = ANY(%s) AND effective_from<=%s
                       AND (effective_to IS NULL OR effective_to>=%s)""",
                (concept_keys, selected_date, selected_date),
            ).fetchall()
            members_by_sector: dict[str, list[dict[str, Any]]] = {}
            for row in memberships:
                members_by_sector.setdefault(str(row["sector_key"]), []).append(dict(row))
            connection.execute(
                """DELETE FROM quant.sector_limit_candidates
                     WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s AND provider_key=%s AND sector_key = ANY(%s)""",
                (selected_date, limit_provider, concept_keys),
            )
            stored = 0
            per_concept: list[dict[str, Any]] = []
            for concept in concepts:
                sector_key = str(concept["sector_key"])
                matches = [(member, limit_by_symbol[str(member["symbol"]).upper()]) for member in members_by_sector.get(sector_key, [])
                           if str(member["symbol"]).upper() in limit_by_symbol]
                matches.sort(key=lambda item: (study_number(item[1].get("limit_amount")) or 0.0,
                                               study_number(item[1].get("pct_chg")) or 0.0), reverse=True)
                selected = matches[:request.leaders_per_concept]
                for member, row in selected:
                    symbol = str(member["symbol"]).upper()
                    connection.execute(
                        """INSERT INTO quant.sector_limit_candidates(taxonomy_key,sector_key,symbol,trading_date,provider_key,available_at,
                                 name,limit_tag,limit_type,pct_change,price,limit_amount,turnover_rate,open_num,status,description,raw)
                           VALUES('ths_concept_flow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(taxonomy_key,sector_key,symbol,trading_date,provider_key) DO UPDATE SET available_at=EXCLUDED.available_at,
                             name=EXCLUDED.name,limit_tag=EXCLUDED.limit_tag,limit_type=EXCLUDED.limit_type,pct_change=EXCLUDED.pct_change,
                             price=EXCLUDED.price,limit_amount=EXCLUDED.limit_amount,turnover_rate=EXCLUDED.turnover_rate,open_num=EXCLUDED.open_num,
                             status=EXCLUDED.status,description=EXCLUDED.description,raw=EXCLUDED.raw""",
                        (sector_key, symbol, selected_date, limit_provider, observed_at, row.get("name"), row.get("tag"), row.get("limit_type"),
                         decimal_or_none(row.get("pct_chg")), decimal_or_none(row.get("price")), decimal_or_none(row.get("limit_amount")),
                         decimal_or_none(row.get("turnover_rate")), decimal_or_none(row.get("open_num")), row.get("status"), row.get("lu_desc"),
                         Json({"limit_list_ths": row, "ths_member": member["raw"],
                               "membership_fetch_status": membership_status.get(sector_key, "unknown")})),
                    )
                    stored += 1
                per_concept.append({"sector_key": sector_key, "label": concept["label"], "net_amount": concept["net_amount"],
                                    "matched_limit_ups": len(matches), "stored": len(selected)})
        return stored, per_concept

    stored, per_concept = await run_database_blocking(persist_candidates)
    failed_members = [item for item in member_results if item["status"] not in {"completed", "unchanged", "empty"}]
    return {"status": "partial" if failed_members else "completed", "trade_date": str(selected_date),
            "concepts": per_concept, "member_results": member_results, "limit_provider": limit_provider,
            "limit_request_key": limit_outcome["request_key"], "limit_rows": len(limit_by_symbol), "candidates": stored}


def market_snapshot_thresholds() -> tuple[int, float, set[str]]:
    return _market_snapshot_actions.thresholds()


def market_snapshot_public_quote_settings() -> dict[str, int | bool]:
    return _market_snapshot_actions.public_quote_settings()


def market_snapshot_tencent_enabled() -> bool:
    return _market_snapshot_actions.tencent_enabled()


def tencent_snapshot_quotes(rows: list[dict[str, Any]], exchange_date: date) -> list[dict[str, Any]]:
    return _market_snapshot_actions.tencent_quotes(rows, exchange_date, intraday_quote_from_tencent)


def realtime_market_session(api_name: str | None = None, now: datetime | None = None) -> tuple[bool, str]:
    return read_realtime_market_session(db, api_name, now)


async def realtime_market_session_async(api_name: str | None = None, now: datetime | None = None) -> tuple[bool, str]:
    return await read_realtime_market_session_async(db, api_name, now, database_runner=run_database_blocking)


def quote_is_for_exchange_date(quote: dict[str, Any], exchange_date: date) -> bool:
    return _market_snapshot_actions.quote_is_for_exchange_date(quote, exchange_date)


def snapshot_universe_symbols(universe_key: str) -> list[str]:
    return _market_snapshot_actions.universe_symbols(universe_key)


def persist_public_quote_batch(provider: str, quotes: list[dict[str, Any]], latency_ms: int | None = None) -> int:
    return _market_snapshot_actions.persist_public_quote_batch(provider, quotes, latency_ms)


def persist_public_quote_failure(provider: str, detail: str, latency_ms: int | None = None) -> None:
    _market_snapshot_actions.persist_public_quote_failure(provider, detail, latency_ms)


def finalize_market_snapshot(
    request: MarketSnapshotRequest,
    observed_at: datetime,
    exchange_date: date,
    symbols: list[str],
    minimum_universe: int,
    minimum_coverage: float,
    licensed_providers: set[str],
    public_quote_settings: dict[str, Any],
    planned_public_requests: int,
    refresh_error: str | None,
    refresh_skipped: str | None,
    tencent_status: dict[str, Any],
) -> dict[str, Any]:
    return _market_snapshot_actions.finalize(
        request, observed_at, exchange_date, symbols, minimum_universe, minimum_coverage,
        licensed_providers, public_quote_settings, planned_public_requests, refresh_error,
        refresh_skipped, tencent_status,
    )


async def build_market_snapshot(request: MarketSnapshotRequest) -> dict[str, Any]:
    """Build a bounded snapshot through the isolated provider/persistence service."""
    return await _market_snapshot_actions.build(
        request,
        run_database=run_database_blocking,
        run_akshare=run_akshare_blocking,
        provider_capabilities=open_provider_capabilities,
        quote_mapper=intraday_quote_from_tencent,
        thresholds=market_snapshot_thresholds,
        public_quote_settings=market_snapshot_public_quote_settings,
        tencent_enabled=market_snapshot_tencent_enabled,
        universe_symbols=snapshot_universe_symbols,
        persist_batch=persist_public_quote_batch,
        persist_failure=persist_public_quote_failure,
        finalize=finalize_market_snapshot,
    )


def announcement_symbols(request: AnnouncementSyncRequest) -> list[str]:
    return _cninfo_announcement_actions.symbols(request)


def persist_announcement_provider_health(status: str, stored: int, failures: list[str],
                                         latency_ms: int | None = None) -> None:
    _cninfo_announcement_actions.persist_provider_health(status, stored, failures, latency_ms)


async def sync_cninfo_announcements(request: AnnouncementSyncRequest) -> dict[str, Any]:
    return await _cninfo_announcement_actions.sync(
        request,
        run_database=run_database_blocking,
        provider_capabilities=open_provider_capabilities,
        symbols=announcement_symbols,
        fetch_announcements=cninfo_announcements,
        persist_events=persist_market_events,
        persist_health=persist_announcement_provider_health,
    )


async def run_post_close_refresh_legacy(request: PostCloseRefreshRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the lease-aware orchestrator."""
    return await run_post_close_refresh(request)
async def run_post_close_refresh(request: PostCloseRefreshRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated refresh orchestrator."""
    trade_date = request.trade_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    full_market_daily_provider = (
        "super_get"
        if (provider := provider_configs().get("super_get"))
        and provider.configured and provider.get_gateway_mode == "promax" and provider.supports("daily")
        else "auto"
    )
    core_symbols: list[str] = []

    def load_core_symbols() -> list[Any]:
        with db.transaction() as connection:
            return connection.execute(
                "SELECT symbol FROM quant.universe_members WHERE universe_key='core' AND enabled "
                "ORDER BY priority,symbol LIMIT %s", (request.announcement_limit,)
            ).fetchall()

    async def akshare_stage() -> dict[str, Any]:
        nonlocal core_symbols
        rows = await run_database_blocking(load_core_symbols)
        core_symbols = [str(row["symbol"]) for row in rows]
        probe_symbol = core_symbols[0] if core_symbols else "000636.SZ"
        return await akshare_probe(AkShareProbeRequest(
            symbol=probe_symbol, trade_date=trade_date,
            include_macro_cross_asset=request.include_macro_cross_asset, board_limit=30,
        ))

    async def announcements_stage() -> dict[str, Any]:
        if not request.include_announcements or not core_symbols:
            return {"status": "skipped", "reason": "disabled or core universe is empty"}
        return await sync_cninfo_announcements(AnnouncementSyncRequest(
            symbols=core_symbols, universe_key="core", start_date=trade_date - timedelta(days=45),
            end_date=trade_date, max_pages_per_symbol=1,
        ))

    actions: dict[str, Callable[[], Any]] = {
        "stale_fetch_runs": lambda: run_database_blocking(reconcile_stale_fetch_runs, FetchRunReconcileRequest(max_age_minutes=90)),
        "analyst_text": lambda: run_database_blocking(reprocess_remote_reports, db, 500),
        "all_a_universe": lambda: sync_market_universe(MarketUniverseSyncRequest()),
        "full_market_daily": lambda: sync_full_market_daily(
            FullMarketDailySyncRequest(trade_date=trade_date, provider=full_market_daily_provider)
        ),
        "index_context": lambda: sync_strategy_index_context(trade_date),
        "close_market_snapshot": lambda: build_market_snapshot(MarketSnapshotRequest(session="close", universe_key="all_a", refresh_public_quotes=True)),
        "akshare_supplements": akshare_stage,
        "ths_industry_flow": lambda: sync_ths_industry_moneyflow(SectorFlowSyncRequest(trade_date=trade_date, provider="super")),
        "ths_concept_flow_and_limit_strength": lambda: sync_ths_concept_signals(SectorFlowSyncRequest(trade_date=trade_date, provider="super")),
        "market_flow_features": lambda: run_database_blocking(
            rebuild_stored_market_flow_features, db, trade_date, trade_date, timeout_seconds=90,
        ),
        "limit_ladder": lambda: refresh_strategy_pattern_sources(trade_date),
        "limit_lift_pattern_mining": lambda: run_strategy_pattern_mining(StrategyPatternMiningRequest(as_of_date=trade_date, refresh_limit_sources=False)),
        "core_daily_controls": lambda: {"status": "skipped"},
        "cninfo_announcements": announcements_stage,
        "board_review": lambda: run_intraday_board_report(deliver=False),
        "close_strategy_decision": lambda: run_strategy_decision(StrategyDecisionRequest(session="close", kind="all", limit=20, validate_tushare_realtime=False)),
        "close_review": lambda: run_database_blocking(_persist_close_review, trade_date),
        "analyst_outcomes": lambda: run_database_blocking(recompute_outcomes, trade_date),
        "analyst_intraday_outcomes": lambda: run_database_blocking(
            recompute_analyst_intraday_outcomes_for_date, trade_date, timeout_seconds=90,
        ),
        "analyst_scorecards": lambda: run_database_blocking(recompute_scorecards, trade_date),
        "analyst_expert_research": lambda: run_database_blocking(rebuild_analyst_research_for_date, trade_date),
        "post_close_strategy": lambda: run_database_blocking(run_post_close_strategy, PostCloseStrategyRequest(as_of_date=trade_date)),
        "watchlist_main_wave": lambda: run_database_blocking(
            persist_watchlist_main_wave_research, WatchlistMainWaveResearchRequest(as_of_date=trade_date),
        ),
        "research_snapshot": lambda: run_database_blocking(build_snapshot, SnapshotRequest(as_of_date=trade_date)),
    }

    async def record_refresh_stage(name: str, stage_date: date, action: Callable[[], Any]) -> Any:
        """Persist and resume one stage without duplicating completed work."""
        return await record_stage_with_receipt(
            name, stage_date, action, db=db, run_database_blocking=run_database_blocking,
            safe_error_detail=safe_error_detail,
        )

    return await run_post_close_refresh_orchestrated(
        request, db=db, lease_key=POST_CLOSE_REFRESH_LEASE_KEY,
        lease_seconds=post_close_refresh_lease_seconds, run_database_blocking=run_database_blocking,
        acquire_lease=acquire_runtime_lease, renew_lease=renew_runtime_lease, release_lease=release_runtime_lease,
        actions=actions, stage_order=(
            "stale_fetch_runs", "analyst_text", "all_a_universe", "full_market_daily", "index_context",
            "close_market_snapshot", "akshare_supplements", "ths_industry_flow", "ths_concept_flow_and_limit_strength",
            "market_flow_features", "limit_ladder", "limit_lift_pattern_mining", "core_daily_controls", "cninfo_announcements",
            "board_review", "close_strategy_decision", "close_review", "analyst_outcomes", "analyst_intraday_outcomes", "analyst_scorecards",
            "analyst_expert_research", "post_close_strategy", "watchlist_main_wave", "research_snapshot",
        ), timeout_overrides={"akshare_supplements": 240.0, "limit_lift_pattern_mining": 120.0},
        record_stage=record_refresh_stage,
        trade_date=trade_date,
        safe_error_detail=safe_error_detail, json_safe=strategy_json_safe,
    )


def _persist_close_review(as_of_date: date) -> dict[str, Any]:
    with db.transaction() as connection:
        return strategy_review_payload(connection, StrategyReviewRequest(session="close", as_of_date=as_of_date, persist=True))


def rebuild_analyst_research_for_date(as_of_date: date) -> dict[str, Any]:
    """Run analyst research inside the service's durable DB transaction."""
    with db.transaction() as connection:
        return rebuild_analyst_research(connection, as_of_date)


def recompute_analyst_intraday_outcomes_for_date(as_of_date: date) -> dict[str, Any]:
    """Settle analyst observations only through the same-day close boundary."""
    cutoff = datetime.combine(
        as_of_date, time(15, 5), tzinfo=ZoneInfo("Asia/Shanghai"),
    ).astimezone(timezone.utc)
    with db.transaction() as connection:
        return materialize_intraday_analyst_outcomes(connection, cutoff_at=cutoff)


async def run_board_research(request: BoardResearchRunRequest) -> dict[str, Any]:
    return await run_board_research_isolated(
        request,
        database=db,
        run_database=run_database_blocking,
        sync_concept_signals=sync_ths_concept_signals,
        sync_concept_limit_candidates=sync_concept_limit_candidates,
        sync_announcements=sync_cninfo_announcements,
        build_stock_study=build_stock_study,
        date_for=tushare_date,
    )


def prepare_tushare_fetch_run(
    request: TushareFetchRequest,
    request_key: str,
    candidate_keys: list[str],
    canonical_params: dict[str, Any],
) -> dict[str, Any] | None:
    """Atomically reuse valid raw evidence or mark a bounded fetch as running."""
    with db.transaction() as connection:
        existing = connection.execute("SELECT status,row_count FROM quant.fetch_runs WHERE request_key=%s", (request_key,)).fetchone()
        if existing and existing["status"] == "completed":
            saved_rows = connection.execute(
                "SELECT provider_key,row_data FROM quant.tushare_raw_records WHERE request_key=%s ORDER BY record_index", (request_key,)
            ).fetchall()
            # Do not reuse a completed ledger entry whose deduplicated raw
            # evidence has later been replaced by a forced refresh.
            if len(saved_rows) == int(existing["row_count"] or 0):
                saved_provider = str(saved_rows[0]["provider_key"]) if saved_rows else candidate_keys[0]
                cached_rows = [dict(row["row_data"]) for row in saved_rows]
                if looks_like_response_header(cached_rows):
                    return {"status": "invalid_response", "api_name": request.api_name, "request_key": request_key,
                            "provider": saved_provider, "stored": existing["row_count"], "normalized_rows": 0,
                            "error": "cached provider response is a header row, not market data"}
                normalized_rows = normalize_tushare_rows(connection, request.api_name, cached_rows, datetime.now(timezone.utc), saved_provider)
                return {"status": "unchanged", "api_name": request.api_name, "request_key": request_key,
                        "provider": saved_provider, "stored": existing["row_count"], "normalized_rows": normalized_rows,
                        "complete": True}
        connection.execute(
            """INSERT INTO quant.fetch_runs(provider_key,capability,request_key,status,attempt_count,started_at,metadata)
               VALUES(%s,%s,%s,'running',1,now(),%s)
               ON CONFLICT(request_key) DO UPDATE SET status='running',attempt_count=quant.fetch_runs.attempt_count+1,
                 started_at=now(),finished_at=null,error_class=null,error_message=null""",
            (candidate_keys[0], request.api_name, request_key,
             Json({"provider": request.provider, "provider_candidates": candidate_keys, "params": canonical_params,
                   "fields": request.fields, "max_rows": request.max_rows, "paginate": request.paginate,
                   "page_size": request.page_size, "max_pages": request.max_pages,
                   "require_complete": request.require_complete})),
        )
    return None


def persist_tushare_fetch_success(
    request: TushareFetchRequest,
    request_key: str,
    bounded_rows: list[dict[str, Any]],
    truncated: bool,
    result: Any,
    provider_latency_ms: int | None = None,
) -> tuple[str, int]:
    """Atomically persist bounded raw evidence, canonical rows and health."""
    with db.transaction() as connection:
        normalized_rows = persist_tushare_rows(
            connection, request.api_name, request_key, bounded_rows, result.provider.key, datetime.now(timezone.utc),
        )
        status = "partial" if truncated else "completed"
        connection.execute(
            """UPDATE quant.fetch_runs SET status=%s,row_count=%s,finished_at=now(),error_class=%s,error_message=%s WHERE request_key=%s""",
            (status, len(bounded_rows), "row_cap" if truncated else None,
             f"response exceeded local cap of {request.max_rows} rows" if truncated else None, request_key),
        )
        for provider_key, error in result.failed_providers:
            record_provider_failure(connection, provider_key, request.api_name, error, provider_latency_ms)
            record_provider_api_capability(connection, provider_key, request.api_name, provider_error_availability(error), note=error)
        for provider_key in result.empty_providers:
            if provider_key != result.provider.key:
                record_provider_api_capability(
                    connection, provider_key, request.api_name, "empty", 0,
                    "Valid empty response; the next audited provider was tried without merging sources.",
                )
        record_provider_success(
            connection, result.provider.key, request.api_name, len(bounded_rows), provider_latency_ms,
        )
        capability_note = "Provider returned real rows; local storage kept a bounded prefix." if truncated else ""
        record_provider_api_capability(connection, result.provider.key, request.api_name,
                                       "verified" if bounded_rows else "empty", len(bounded_rows), capability_note)
    return status, normalized_rows


def persist_tushare_fetch_cancel(request_key: str, api_name: str, candidate_keys: list[str]) -> None:
    """Close a caller-cancelled fetch without blaming an upstream provider.

    ``asyncio.wait_for`` is used by bounded study/UI workflows.  Its timeout
    cancels our coroutine before a provider result is known, so recording a
    provider failure here would turn local latency budget pressure into a
    false circuit-open event.
    """
    with db.transaction() as connection:
        connection.execute(
            "UPDATE quant.fetch_runs SET status='blocked',finished_at=now(),error_class='caller_cancelled',error_message='Request cancelled by the caller timeout before provider outcome' WHERE request_key=%s",
            (request_key,),
        )


def persist_tushare_fetch_failure(request_key: str, api_name: str, candidate_keys: list[str], error: Exception,
                                  provider_latency_ms: int | None = None) -> None:
    safe_error = safe_error_detail(str(error), 1000)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE quant.fetch_runs SET status='failed',finished_at=now(),error_class='provider_error',error_message=%s WHERE request_key=%s",
            (safe_error, request_key),
        )
        provider_failures = error.failures if isinstance(error, ProviderCallError) and error.failures else tuple(
            (provider_key, safe_error_detail(str(error))) for provider_key in candidate_keys
        )
        for provider_key, provider_error in provider_failures:
            safe_provider_error = safe_error_detail(str(provider_error))
            record_provider_failure(connection, provider_key, api_name, safe_provider_error, provider_latency_ms)
            record_provider_api_capability(connection, provider_key, api_name,
                                           provider_error_availability(safe_provider_error), note=safe_provider_error)


def persist_tushare_fetch_blocked(request_key: str, error: Exception) -> None:
    """Close a ledger row for local backpressure without blaming a provider."""
    detail = safe_error_detail(str(error), 300)
    with db.transaction() as connection:
        connection.execute(
            """UPDATE quant.fetch_runs SET status='blocked',finished_at=now(),
               error_class='local_capacity',error_message=%s WHERE request_key=%s""",
            (detail, request_key),
        )


async def fetch_tushare_catalog(request: TushareFetchRequest) -> dict[str, Any]:
    if request.api_name in REALTIME_MARKET_HOURS_APIS:
        active, reason = await realtime_market_session_async(request.api_name)
        if not active:
            raise HTTPException(status_code=409, detail=f"{request.api_name} probe skipped: {reason}")
    canonical_params = json.loads(json.dumps(request.params, ensure_ascii=False, sort_keys=True, default=str))
    candidates = provider_candidates(request.api_name, request.provider)
    if not candidates:
        raise HTTPException(status_code=503, detail=f"no configured provider supports {request.api_name} for {request.provider}")
    blocked_provider_keys = await circuit_open_provider_keys_async(request.api_name, candidates)
    candidates = [provider for provider in candidates if provider.key not in blocked_provider_keys]
    if not candidates:
        raise HTTPException(status_code=503, detail=f"all configured providers are temporarily circuit-open for {request.api_name}")
    candidate_keys = [provider.key for provider in candidates]
    request_identity: dict[str, Any] = {"api_name": request.api_name, "provider": request.provider,
                                        "provider_candidates": candidate_keys, "params": canonical_params,
                                        "fields": request.fields, "paginate": request.paginate,
                                        "page_size": request.page_size, "max_pages": request.max_pages,
                                        "require_complete": request.require_complete}
    if request.force_refresh:
        request_identity["audit_nonce"] = uuid.uuid4().hex
    request_key = hashlib.sha256(json.dumps(request_identity, sort_keys=True).encode()).hexdigest()
    cached = await run_database_blocking(
        prepare_tushare_fetch_run, request, request_key, candidate_keys, canonical_params, timeout_seconds=60,
    )
    if cached is not None:
        return cached
    provider_started_at = asyncio.get_running_loop().time()
    try:
        result = await call_tushare_api(
            request.api_name, canonical_params, request.fields, request.provider,
            paginate=request.paginate, page_size=request.page_size,
            max_rows=request.max_rows, max_pages=request.max_pages,
            require_complete=request.require_complete, blocked_provider_keys=blocked_provider_keys,
        )
        rows = result.rows
        if looks_like_response_header(rows):
            raise ProviderCallError("provider returned a header row instead of market data")
        if not realtime_rows_are_current(request.api_name, rows):
            raise ProviderCallError(f"provider returned stale realtime rows for {request.api_name}")
        if request.api_name.endswith("_min") or request.api_name.endswith("_min_daily"):
            # Some verified gateways return intraday bars from open to now.
            # Keep the newest bounded rows so short UI/study requests never
            # accidentally retain only the 09:31 opening bars.
            rows = sorted(rows, key=lambda row: str(
                row.get("time") or row.get("updated_at") or row.get("trade_time")
                or row.get("datetime") or ""
            ), reverse=True)
        truncated = len(rows) > request.max_rows or not result.complete
        if request.require_complete and truncated:
            raise ProviderCallError(
                f"{result.provider.key} did not reach a terminal page for {request.api_name} "
                f"within {request.max_pages} pages/{request.max_rows} rows"
            )
        bounded_rows = rows[:request.max_rows]
        provider_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)
        status, normalized_rows = await run_database_blocking(
            persist_tushare_fetch_success, request, request_key, bounded_rows, truncated, result, provider_latency_ms,
            timeout_seconds=60,
        )
        return {"status": status, "api_name": request.api_name, "group": TUSHARE_CATALOG[request.api_name],
                "normalized": request.api_name in CORE_NORMALIZED_APIS, "received": len(rows), "stored": len(bounded_rows),
                "provider": result.provider.key, "fallback_failures": [{"provider": key, "error": error} for key, error in result.failed_providers],
                "fallback_empty_providers": list(result.empty_providers),
                "normalized_rows": normalized_rows, "truncated": truncated, "complete": not truncated,
                "pages": result.pages, "request_key": request_key}
    except asyncio.CancelledError:
        # A bounded study/probe may cancel this coroutine while a provider is
        # still waiting on the network.  Do not leave the durable run looking
        # active: that would incorrectly make an operational timeout appear
        # as an in-flight market-data fetch.
        await run_database_blocking(persist_tushare_fetch_cancel, request_key, request.api_name, candidate_keys)
        raise
    except ExecutorSaturatedError as error:
        # The Super GET proxy worker and local DB work are deliberately bounded.
        # A rejected local submission is neither an upstream failure nor evidence
        # that a provider capability is bad, so it must not advance its circuit.
        await run_database_blocking(persist_tushare_fetch_blocked, request_key, error)
        raise HTTPException(
            status_code=503,
            detail=LOCAL_CAPACITY_HTTP_DETAIL,
        ) from error
    except Exception as error:  # noqa: BLE001 - store an actionable, token-free failure
        provider_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)
        await run_database_blocking(
            persist_tushare_fetch_failure, request_key, request.api_name, candidate_keys, error, provider_latency_ms,
        )
        raise HTTPException(status_code=502, detail=f"Tushare {request.api_name} request failed") from error


def tushare_rows_for_request(request_key: str) -> list[dict[str, Any]]:
    """Read the immutable raw evidence associated with one bounded fetch."""
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT row_data FROM quant.tushare_raw_records WHERE request_key=%s ORDER BY record_index",
            (request_key,),
        ).fetchall()
    return [dict(row["row_data"]) for row in rows]


async def stock_study_fetch(label: str, request: TushareFetchRequest) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch one study input without allowing a failed enrichment to abort research."""
    try:
        outcome = await asyncio.wait_for(fetch_tushare_catalog(request), timeout=12)
        rows = await run_database_blocking(tushare_rows_for_request, str(outcome["request_key"]))
        if looks_like_response_header(rows):
            return ({"source": label, "api_name": request.api_name, "provider": outcome.get("provider"),
                     "status": "invalid_response", "received": 0, "stored": outcome.get("stored", 0),
                     "error": "provider returned a header row instead of market data"}, [])
        return ({"source": label, "api_name": request.api_name, "provider": outcome.get("provider"),
                 "status": outcome["status"], "received": outcome.get("received", outcome.get("stored", 0)),
                 "stored": outcome.get("stored", 0), "fallback_failures": outcome.get("fallback_failures", [])}, rows)
    except asyncio.TimeoutError:
        return ({"source": label, "api_name": request.api_name, "provider": request.provider,
                 "status": "blocked", "received": 0, "stored": 0,
                 "error": "study source exceeded 12 second local budget; provider outcome was not observed"}, [])
    except HTTPException as error:
        status = "blocked" if is_local_capacity_http_error(error) else "circuit_open" if is_circuit_open_http_error(error) else "failed"
        return ({"source": label, "api_name": request.api_name, "provider": request.provider,
                 "status": status, "received": 0, "stored": 0, "error": str(error.detail)}, [])


def persist_stock_study_free_result(provider: str, capability: str, payload: Any, symbol: str,
                                    latency_ms: int | None = None) -> int:
    if isinstance(payload, list):
        stored = persist_free_daily(provider, payload) if capability == "daily_bar" else len(payload)
    else:
        stored = persist_free_quote(provider, symbol, payload) if capability == "realtime_quote" else int(bool(payload))
    with db.transaction() as connection:
        record_provider_success(connection, provider, capability, stored, latency_ms)
    return stored


def persist_stock_study_free_failure(provider: str, capability: str, error: str,
                                     latency_ms: int | None = None) -> None:
    with db.transaction() as connection:
        record_provider_failure(connection, provider, capability, error, latency_ms)


async def stock_study_free_fetch(label: str, provider: str, capability: str, fetcher: Any, symbol: str) -> tuple[dict[str, Any], Any]:
    """Run one token-free public probe and preserve the independent evidence."""
    if capability in await open_provider_capabilities(provider, [capability]):
        return (
            {"source": label, "api_name": capability, "provider": provider, "status": "circuit_open",
             "received": 0, "stored": 0, "error": "provider health circuit is open; upstream request skipped"},
            [] if capability == "daily_bar" else None,
        )
    try:
        started_at = asyncio.get_running_loop().time()
        # Accept a factory instead of an already-created coroutine.  This is
        # essential for circuit-open calls: no coroutine/HTTP request exists
        # until the durable provider-health check allows it.
        payload = await asyncio.wait_for(fetcher(), timeout=10)
        if isinstance(payload, list):
            received = len(payload)
        else:
            received = int(bool(payload))
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        stored = await run_database_blocking(
            persist_stock_study_free_result, provider, capability, payload, symbol, latency_ms, timeout_seconds=60,
        )
        return ({"source": label, "api_name": capability, "provider": provider, "status": "completed" if received else "empty",
                 "received": received, "stored": stored}, payload)
    except ExecutorSaturatedError as error:
        return ({"source": label, "api_name": capability, "provider": provider, "status": "blocked", "received": 0,
                 "stored": 0, "error": safe_error_detail(str(error), 300)}, [] if capability == "daily_bar" else None)
    except (asyncio.TimeoutError, httpx.HTTPError, FreeProviderError, AkShareProviderError, ValueError) as error:
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database_blocking(
            persist_stock_study_free_failure, provider, capability,
            str(error) or "public provider request timed out", latency_ms,
        )
        return ({"source": label, "api_name": capability, "provider": provider, "status": "failed", "received": 0, "stored": 0,
                 "error": str(error) or "public provider request timed out"}, [] if capability == "daily_bar" else None)


def study_number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def study_date_key(row: dict[str, Any]) -> str:
    return str(row.get("trade_date") or row.get("date") or "")


def latest_study_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(rows, key=study_date_key) if rows else None


def raw_api_window_summary(connection: Any, api_name: str, symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    row = connection.execute(
        """SELECT count(*)::int rows,max(row_data->>'trade_date') latest_date
             FROM quant.tushare_raw_records
            WHERE api_name=%s AND row_data->>'ts_code'=%s
              AND row_data->>'trade_date' BETWEEN %s AND %s""",
        (api_name, symbol, start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")),
    ).fetchone()
    return {"rows": int(row["rows"] or 0), "latest_date": row["latest_date"]}


def stock_window_readiness(symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    """Report whether one stock has enough recent evidence for on-demand study."""
    specs = [
        ("daily", "日线行情", "P0"),
        ("daily_basic", "估值与换手", "P0"),
        ("stk_limit", "涨跌停价格", "P0"),
        ("moneyflow_dc", "东财主力/散户资金", "P0"),
        ("adj_factor", "复权因子", "P1"),
        ("moneyflow", "Tushare资金流", "P1"),
        ("moneyflow_ths", "同花顺资金流", "P1"),
        ("cyq_perf", "筹码胜率摘要", "P1"),
        ("cyq_chips", "筹码分布明细", "P1"),
        ("stk_factor_pro", "专业技术因子", "P1"),
    ]
    with db.transaction() as connection:
        items: list[dict[str, Any]] = []
        for api_name, label, priority in specs:
            if api_name == "daily":
                row = connection.execute(
                    """SELECT count(*)::int rows,max(trading_date) latest_date
                         FROM quant.canonical_bars_daily
                        WHERE symbol=%s AND trading_date BETWEEN %s AND %s""",
                    (symbol, start_date, end_date),
                ).fetchone()
                rows, latest_date = int(row["rows"] or 0), row["latest_date"]
            elif api_name == "daily_basic":
                row = connection.execute(
                    """SELECT count(*)::int rows,max(trading_date) latest_date
                         FROM quant.daily_fundamentals
                        WHERE symbol=%s AND trading_date BETWEEN %s AND %s""",
                    (symbol, start_date, end_date),
                ).fetchone()
                rows, latest_date = int(row["rows"] or 0), row["latest_date"]
            elif api_name == "stk_limit":
                row = connection.execute(
                    """SELECT count(*)::int rows,max(trading_date) latest_date
                         FROM quant.daily_trade_limits
                        WHERE symbol=%s AND trading_date BETWEEN %s AND %s""",
                    (symbol, start_date, end_date),
                ).fetchone()
                rows, latest_date = int(row["rows"] or 0), row["latest_date"]
            elif api_name == "adj_factor":
                row = connection.execute(
                    """SELECT count(*)::int rows,max(trading_date) latest_date
                         FROM quant.daily_adjustment_factors
                        WHERE symbol=%s AND trading_date BETWEEN %s AND %s""",
                    (symbol, start_date, end_date),
                ).fetchone()
                rows, latest_date = int(row["rows"] or 0), row["latest_date"]
            else:
                summary = raw_api_window_summary(connection, api_name, symbol, start_date, end_date)
                rows, latest_date = summary["rows"], summary["latest_date"]
            status = "ready" if rows > 0 else "missing"
            items.append({"api_name": api_name, "label": label, "priority": priority, "rows": rows,
                          "latest_date": str(latest_date) if latest_date else None, "status": status})
    blockers = [item["api_name"] for item in items if item["priority"] == "P0" and item["status"] != "ready"]
    return {"symbol": symbol, "window_start": str(start_date), "window_end": str(end_date),
            "mode": "on_demand_single_stock_window", "decision_ready": not blockers,
            "blockers": blockers, "items": items}


def stock_study_claims(symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT c.claim_id,a.name analyst_name,c.subject_label,c.direction,c.strength,c.horizon_days,
                      c.extraction_confidence,c.available_at,left(e.body,500) evidence
                 FROM quant.analyst_claims c JOIN quant.remote_analysts a ON a.remote_analyst_id=c.remote_analyst_id
                 JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
                WHERE c.scope='stock' AND c.subject_key=%s AND c.available_at<=now()
                ORDER BY c.available_at DESC,c.created_at DESC LIMIT 50""",
            (symbol,),
        ).fetchall()
    claims = [dict(row) for row in rows]
    denominator = sum(float(row["strength"] or 0) * float(row["extraction_confidence"] or 0) for row in claims)
    weighted = sum(float(row["direction"] or 0) * float(row["strength"] or 0) * float(row["extraction_confidence"] or 0) for row in claims)
    normalized = round(weighted / denominator, 4) if denominator else 0.0
    direction = "positive" if normalized >= 0.2 else "negative" if normalized <= -0.2 else "neutral"
    return claims, {"claim_count": len(claims), "positive": sum(1 for row in claims if row["direction"] > 0),
                    "negative": sum(1 for row in claims if row["direction"] < 0), "neutral": sum(1 for row in claims if row["direction"] == 0),
                    "score": normalized, "direction": direction}


async def build_stock_study(symbol: str, request: StockStudyRequest) -> dict[str, Any]:
    as_of = request.as_of_date or cn_today()
    market_date = as_of - timedelta(days=2) if as_of.weekday() == 6 else as_of - timedelta(days=1) if as_of.weekday() == 5 else as_of
    # The requested window is in trading days. Calendar slack covers weekends
    # and preserves enough observations for the SMA20 calculation.
    calendar_span = min(45, max(request.lookback_days + 12, 32))
    start = (market_date - timedelta(days=calendar_span)).strftime("%Y%m%d")
    end = market_date.strftime("%Y%m%d")
    dated = {"ts_code": symbol, "start_date": start, "end_date": end}
    fetches = [
        ("主 Tushare 日线", TushareFetchRequest(api_name="daily", provider="primary", params=dated, fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount", max_rows=60)),
        ("超级源日线", TushareFetchRequest(api_name="daily", provider="super", params=dated, fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount", max_rows=60)),
        ("REST 备用基础信息", TushareFetchRequest(api_name="stock_basic", provider="backup", params={"ts_code": symbol, "limit": 3}, max_rows=3)),
        ("复权因子", TushareFetchRequest(api_name="adj_factor", params=dated, max_rows=60)),
        ("每日估值指标", TushareFetchRequest(api_name="daily_basic", params=dated, max_rows=60)),
        ("涨跌停价格", TushareFetchRequest(api_name="stk_limit", params=dated, max_rows=60)),
        ("个股资金流", TushareFetchRequest(api_name="moneyflow", params=dated, max_rows=60)),
        ("同花顺个股资金流", TushareFetchRequest(api_name="moneyflow_ths", params=dated, max_rows=60)),
        ("东财个股资金流", TushareFetchRequest(api_name="moneyflow_dc", params=dated, max_rows=60)),
        ("筹码及胜率", TushareFetchRequest(api_name="cyq_perf", params=dated, max_rows=60)),
        ("筹码分布", TushareFetchRequest(api_name="cyq_chips", params=dated, max_rows=500)),
        ("技术因子专业版", TushareFetchRequest(api_name="stk_factor_pro", params=dated, max_rows=60)),
    ]
    realtime_active, realtime_reason = await realtime_market_session_async()
    if realtime_active:
        fetches.extend([
            ("主源实时分钟", TushareFetchRequest(api_name="rt_min", provider="primary", params={"ts_code": symbol, "freq": "1MIN"}, max_rows=3)),
            ("超级源实时分钟", TushareFetchRequest(api_name="rt_min", provider="super", params={"ts_code": symbol, "freq": "1MIN"}, max_rows=3)),
        ])
    # The inputs are independent and each adapter has its own timeout.  Run the
    # bounded probes concurrently so one slow enrichment cannot consume the UI
    # request budget for the whole study.
    baostock_task = asyncio.create_task(sync_baostock(TushareSyncRequest(trade_date=market_date, symbols=[symbol])))
    results = await asyncio.gather(*(stock_study_fetch(label, payload) for label, payload in fetches))
    free_results = await asyncio.gather(
        stock_study_free_fetch("东方财富公开日线", "eastmoney_free", "daily_bar", lambda: eastmoney_daily(symbol, start, end), symbol),
        stock_study_free_fetch("东方财富公开报价", "eastmoney_free", "realtime_quote", lambda: eastmoney_quote(symbol), symbol),
        stock_study_free_fetch("AKShare公开日线", "akshare", "daily_bar", lambda: run_akshare_blocking(akshare_daily, symbol, start, end, timeout_seconds=12), symbol),
        stock_study_free_fetch("腾讯财经公开日线", "tencent_free", "daily_bar", lambda: tencent_daily(symbol, start, end), symbol),
        stock_study_free_fetch("新浪财经公开报价", "sina_free", "realtime_quote", lambda: sina_quote(symbol), symbol),
    )
    sources = [result[0] for result in results]
    if not realtime_active:
        sources.extend([
            {"source": "主源实时分钟", "api_name": "rt_min", "provider": "primary", "status": "skipped", "received": 0, "stored": 0, "error": realtime_reason},
            {"source": "超级源实时分钟", "api_name": "rt_min", "provider": "super", "status": "skipped", "received": 0, "stored": 0, "error": realtime_reason},
        ])
    sources.extend(result[0] for result in free_results)
    data = {label: rows for (label, _), (_, rows) in zip(fetches, results, strict=True)}
    free_data = {result[0]["source"]: result[1] for result in free_results}
    try:
        baostock = await asyncio.wait_for(baostock_task, timeout=15)
    except asyncio.TimeoutError:
        baostock = {"status": "failed", "imported": 0, "failures": ["study source exceeded 15 second budget"]}
    sources.append({"source": "Baostock 日线", "api_name": "daily_bar", "provider": "baostock", "status": baostock["status"],
                    "received": baostock.get("imported", 0), "stored": baostock.get("imported", 0), "failures": baostock.get("failures", [])})
    announcement_started_at = asyncio.get_running_loop().time()
    try:
        announcement_rows = await asyncio.wait_for(cninfo_announcements(symbol, datetime.strptime(start, "%Y%m%d").date(), market_date, max_pages=1), timeout=12)
        announcement_stored = await run_database_blocking(persist_market_events, "cninfo_free", announcement_rows, timeout_seconds=60)
        await run_database_blocking(
            persist_announcement_provider_health, "completed", announcement_stored, [],
            round((asyncio.get_running_loop().time() - announcement_started_at) * 1000),
        )
        sources.append({"source": "巨潮公开公告", "api_name": "announcement", "provider": "cninfo_free",
                        "status": "completed" if announcement_rows else "empty", "received": len(announcement_rows), "stored": announcement_stored})
    except Exception as error:  # noqa: BLE001
        announcement_rows = []
        await run_database_blocking(
            persist_announcement_provider_health, "failed", 0, [str(error)],
            round((asyncio.get_running_loop().time() - announcement_started_at) * 1000),
        )
        sources.append({"source": "巨潮公开公告", "api_name": "announcement", "provider": "cninfo_free",
                        "status": "failed", "received": 0, "stored": 0, "error": str(error)[:300]})
    daily_rows = data["主 Tushare 日线"] or data["超级源日线"]
    technical = technical_summary(daily_rows)
    claims, analyst = await run_database_blocking(stock_study_claims, symbol)
    announcements = await run_database_blocking(recent_market_events, symbol, 20)
    technical_component = ((technical["score"] - 50) / 50) if technical.get("score") is not None else 0.0
    combined_score = round(max(0, min(100, 50 + technical_component * 25 + analyst["score"] * 25)), 1)
    stance = "research_positive" if combined_score >= 62 else "research_negative" if combined_score <= 38 else "mixed_or_insufficient"
    profile = latest_study_row(data["REST 备用基础信息"])
    readiness = await run_database_blocking(
        stock_window_readiness, symbol, datetime.strptime(start, "%Y%m%d").date(), market_date,
    )
    return {"symbol": symbol, "as_of_date": str(market_date), "lookback_days": request.lookback_days, "sources": sources,
            "on_demand_readiness": readiness,
            "market": {"daily_bars": daily_rows[-45:], "latest_realtime": latest_study_row(data.get("主源实时分钟", []) or data.get("超级源实时分钟", [])),
                       "eastmoney_quote": free_data["东方财富公开报价"], "eastmoney_daily_bars": free_data["东方财富公开日线"],
                       "akshare_daily_bars": free_data["AKShare公开日线"],
                       "tencent_daily_bars": free_data["腾讯财经公开日线"], "sina_quote": free_data["新浪财经公开报价"],
                       "latest_adj_factor": latest_study_row(data["复权因子"]), "latest_limit": latest_study_row(data["涨跌停价格"]),
                       "latest_daily_basic": latest_study_row(data["每日估值指标"]), "latest_moneyflow": latest_study_row(data["个股资金流"]),
                       "latest_ths_moneyflow": latest_study_row(data["同花顺个股资金流"]),
                       "latest_dc_moneyflow": latest_study_row(data["东财个股资金流"]),
                       "latest_chip": latest_study_row(data["筹码及胜率"]), "latest_chip_distribution": latest_study_row(data["筹码分布"]),
                       "latest_factor": latest_study_row(data["技术因子专业版"]), "profile": profile},
            "events": {"announcements": announcements, "provider": "cninfo_free", "decision_eligible": False},
            "technical": technical, "analyst": {"summary": analyst, "claims": claims},
            "combined": {"score": combined_score, "stance": stance, "notice": "研究结论基于当前可得数据与远端分析师证据，不构成交易指令。",
                         "reasons": [*technical.get("reasons", [])[:3], f"远端分析师有效观点 {analyst['claim_count']} 条，聚合方向为 {analyst['direction']}"]}}


async def sync_tushare_daily_core(as_of_date: date, requested_symbols: list[str] | None = None) -> dict[str, Any]:
    """Fetch only the small daily control-plane needed by the rule baseline."""
    symbols = [symbol for symbol in await resolve_sync_symbols_async(requested_symbols or []) if symbol != "000300.SH"]
    if not symbols:
        return {"status": "disabled", "reason": "no explicit equity universe", "requests": []}
    stamp = as_of_date.strftime("%Y%m%d")
    calendar_start = date(as_of_date.year, 1, 1).strftime("%Y%m%d")
    calendar_end = date(as_of_date.year, 12, 31).strftime("%Y%m%d")
    requests = [TushareFetchRequest(
        api_name="trade_cal",
        params={"exchange": "SSE", "start_date": calendar_start, "end_date": calendar_end},
        max_rows=400,
    )]
    for symbol in symbols:
        shared = {"ts_code": symbol, "start_date": stamp, "end_date": stamp}
        requests.extend([
            TushareFetchRequest(api_name="daily_basic", params=shared, max_rows=10),
            TushareFetchRequest(api_name="adj_factor", params=shared, max_rows=10),
            TushareFetchRequest(api_name="stk_limit", params=shared, max_rows=10),
            TushareFetchRequest(api_name="suspend_d", params={"ts_code": symbol, "trade_date": stamp}, max_rows=10),
        ])
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for request in requests:
        try:
            results.append(await fetch_tushare_catalog(request))
        except HTTPException:
            failures.append(request.api_name)
    return {"status": "completed" if not failures else "partial", "symbols": symbols, "requests": results, "failures": failures}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.open()
    await async_db.open()
    configure_provider_request_reserver(
        reserve_tushare_provider_request_slot,
        max_wait_seconds=provider_global_rate_limit_max_wait_seconds(),
    )
    for configured_provider in provider_configs().values():
        provider_shared_rate_limit_wait_seconds.labels(configured_provider.key)
        provider_shared_rate_limit_rejections_total.labels(configured_provider.key)
    await start_http_clients()
    if legacy_schema_bootstrap_enabled():
        db.migrate()
    db.verify_versioned_schema()
    # Catalog registration is bounded local database work, but FastAPI startup
    # still runs on the event loop.  Keep it on the same executor boundary as
    # the background loops it enables.
    await run_database_blocking(ensure_catalog_capabilities, timeout_seconds=30)
    interval_seconds = intraday_scan_interval_seconds()
    lease_holder_id = uuid.uuid4()
    lease_seconds = background_loop_lease_seconds()

    async def leased_background_loop(label: str, factory: Callable[[], Awaitable[None]]) -> None:
        lease_key = f"background_loop:{label}"
        async def acquire() -> bool:
            return await run_database_blocking(acquire_runtime_lease, db, lease_key, lease_holder_id, lease_seconds)
        async def renew() -> bool:
            return await run_database_blocking(renew_runtime_lease, db, lease_key, lease_holder_id, lease_seconds)
        async def release() -> None:
            await run_database_blocking(release_runtime_lease, db, lease_key, lease_holder_id)
        await supervise_leased_loop(
            label, factory, acquire, renew, release, lease_seconds,
            on_state=background_loop_registry.mark,
        )

    monitor_task = asyncio.create_task(leased_background_loop("intraday_monitor", lambda: intraday_monitor_loop(interval_seconds))) if interval_seconds >= 30 else None
    fast_quote_task = asyncio.create_task(leased_background_loop("super_get_fast_quote", intraday_super_get_fast_quote_loop)) if interval_seconds >= 30 else None
    review_task = asyncio.create_task(leased_background_loop("strategy_review", strategy_review_loop)) if strategy_review_automation_enabled() else None
    post_close_strategy_task = asyncio.create_task(leased_background_loop("post_close_strategy", post_close_strategy_loop)) if post_close_strategy_automation_enabled() else None
    daily_summary_task = asyncio.create_task(leased_background_loop("daily_strategy_summary", daily_strategy_summary_loop)) if daily_summary_automation_enabled() else None
    member_backfill_task = asyncio.create_task(leased_background_loop("ths_member_backfill", ths_concept_member_backfill_loop)) if ths_concept_member_backfill_enabled() else None
    all_member_backfill_task = asyncio.create_task(leased_background_loop("all_board_member_backfill", all_board_member_backfill_loop)) if all_board_member_backfill_enabled() else None
    minute_profile_task = asyncio.create_task(leased_background_loop("minute_profile_capture", intraday_minute_profile_capture_loop)) if intraday_minute_profile_capture_enabled() else None
    order_book_task = asyncio.create_task(leased_background_loop("tencent_order_book", intraday_order_book_loop)) if intraday_order_book_enabled() and interval_seconds >= 30 else None
    board_curve_task = asyncio.create_task(leased_background_loop("board_flow_curve", intraday_board_flow_curve_loop)) if intraday_board_curve_enabled() else None
    try:
        yield
    finally:
        for task in (monitor_task, fast_quote_task, review_task, post_close_strategy_task, daily_summary_task, member_backfill_task, all_member_backfill_task, minute_profile_task, order_book_task, board_curve_task):
            if task is not None:
                task.cancel()
        for task in (monitor_task, fast_quote_task, review_task, post_close_strategy_task, daily_summary_task, member_backfill_task, all_member_backfill_task, minute_profile_task, order_book_task, board_curve_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _intraday_all_a_snapshots.cancel_inflight()
        shutdown_super_get_executor()
        shutdown_runtime_executors()
        await close_http_clients()
        configure_provider_request_reserver(None)
        await async_db.close()
        db.close()


app = FastAPI(title="Market Research Service", version="0.1.0", lifespan=lifespan)

# Prometheus normally scrapes ``/metrics`` rather than ``/health``.  Keep the
# local control-plane gauges current for that normal path too, without turning
# every scrape into an unbounded database probe.
_METRICS_CONTROL_PLANE_REFRESH_SECONDS = 5.0
_metrics_control_plane_lock = threading.Lock()
_metrics_control_plane_refreshed_at = 0.0
background_loop_registry = LoopRuntimeRegistry()


def refresh_metrics_control_plane(*, now: float | None = None) -> bool:
    """Refresh pool/circuit gauges from local state at most once per short TTL.

    This intentionally has no provider call.  A transient local database
    problem must not make Prometheus itself fail; ``/health`` remains the
    strict diagnostic endpoint that reports a database outage to callers.
    """
    global _metrics_control_plane_refreshed_at
    observed_at = monotonic() if now is None else now
    if observed_at - _metrics_control_plane_refreshed_at < _METRICS_CONTROL_PLANE_REFRESH_SECONDS:
        return False
    if not _metrics_control_plane_lock.acquire(blocking=False):
        return False
    try:
        observed_at = monotonic() if now is None else now
        if observed_at - _metrics_control_plane_refreshed_at < _METRICS_CONTROL_PLANE_REFRESH_SECONDS:
            return False
        try:
            pool = db.pool_status()
            db_pool_connections.labels("size").set(pool["pool_size"])
            db_pool_connections.labels("available").set(pool["available"])
            db_pool_connections.labels("waiting").set(pool["waiting"])
            with db.transaction() as connection:
                open_circuits = connection.execute(
                    "SELECT count(*)::int AS count FROM quant.provider_health WHERE circuit_open_until > now()"
                ).fetchone()["count"]
            provider_circuit_open.set(int(open_circuits))
        except Exception:  # noqa: BLE001 - metrics must remain scrapeable during a local outage
            return False
        _metrics_control_plane_refreshed_at = observed_at
        return True
    finally:
        _metrics_control_plane_lock.release()


@app.exception_handler(ExecutorSaturatedError)
async def executor_saturated_response(_: Request, __: ExecutorSaturatedError) -> JSONResponse:
    """Expose local backpressure as a retryable service state, never a 500."""
    return JSONResponse(
        status_code=503,
        content={"detail": "local processing capacity is temporarily saturated; retry shortly"},
    )


app.include_router(build_provider_status_router(db, provider_status, free_provider_status))
app.include_router(build_research_readiness_router(
    db, historical_estimate_from_db, feature_readiness_state, historical_replay_readiness, async_db,
))
app.include_router(build_analyst_reads_router(db, remote_report_list_state, analyst_text_factor_summary))
app.include_router(build_analyst_trade_action_reads_router(db, anqiang_trade_action_replay))
app.include_router(build_analyst_action_outcomes_router(db, materialize_anqiang_action_replay_outcomes))
app.include_router(build_analyst_skill_reads_router(db, analyst_skill_profiles))
app.include_router(build_analyst_research_reads_router(db, analyst_research_status))
app.include_router(build_automation_reads_router(db))
app.include_router(build_event_reads_router(db, async_db))
app.include_router(build_strategy_reads_router(db, STRATEGY_DECISION_MODEL_VERSION, async_db))
app.include_router(build_paper_reads_router(db, async_db))
app.include_router(build_paper_actions_router(db, configure_paper_account, accept_paper_decision))
app.include_router(build_analyst_prompt_lab_router(
    db, materialize_prompt_candidates, label_prompt_candidate, evaluate_prompt_variant,
    materialize_intraday_analyst_outcomes,
))
app.include_router(build_strategy_pattern_reads_router(
    db, merge_limit_pool_sources, limit_board_count, strategy_json_safe,
    post_close_limit_daily_features, post_close_exact_board_context, post_close_tushare_lhb_context, async_db,
    run_database_blocking,
))
app.include_router(build_board_rotation_reads_router(db))
app.include_router(build_board_stock_mining_reads_router(db))
app.include_router(build_limit_linkage_mining_reads_router(db))
app.include_router(build_board_curve_reads_router(db, intraday_board_curve_retention_days, intraday_board_rotation_retention_days))
app.include_router(build_market_flow_reads_router(db))
app.include_router(build_research_catalog_reads_router(db, async_db))
app.include_router(build_intraday_outcome_reads_router(
    db, intraday_point_in_time_market_context_batch, intraday_signal_attribution, intraday_outcome_attribution_summary,
    async_database=async_db,
    market_context_from_board_report_fn=intraday_market_context_from_board_report,
))
app.include_router(build_sector_reads_router(db, ths_concept_member_backfill_enabled, ths_concept_member_backfill_batch_size))
app.include_router(build_intraday_evidence_reads_router(
    db, intraday_decision_card, async_database=async_db,
))
app.include_router(build_market_result_reads_router(
    db, TUSHARE_CATALOG, current_data_coverage, feature_readiness_state,
    lambda: historical_estimate_from_db(HistoricalCoverageEstimateRequest(years=3, include_minute=False)),
    offline_data_root, analyst_scorecard_readiness, async_db,
))


@app.middleware("http")
async def require_quant_write_key(request: Request, call_next: Any) -> Any:
    configured_key = os.getenv("QUANT_WRITE_API_KEY", "").strip()
    supplied_key = request.headers.get("X-Quant-Write-Key")
    if not write_access_allowed(request.method, supplied_key, configured_key) and not remote_archive_sync_bearer_allowed(request):
        return JSONResponse(status_code=401, content={"detail": "valid X-Quant-Write-Key is required for write operations"})
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, Any]:
    """Expose local runtime evidence without touching market providers."""
    def set_db_pool_gauge(pool: dict[str, Any]) -> None:
        db_pool_connections.labels("size").set(pool["pool_size"])
        db_pool_connections.labels("available").set(pool["available"])
        db_pool_connections.labels("waiting").set(pool["waiting"])

    try:
        return read_health_payload(HealthDependencies(
            database=db, post_close_lease_key=POST_CLOSE_REFRESH_LEASE_KEY,
            background_loop_lease_seconds=background_loop_lease_seconds,
            data_directory=lambda: Path(os.getenv("QUANT_DATA_DIR", "/var/lib/quant")),
            resource_status=runtime_resource_status, public_http_client_status=public_http_client_status,
            alert_http_client_status=alert_http_client_status, provider_http_client_status=provider_http_client_status,
            remote_archive_http_client_status=remote_archive_http_client_status,
            network_status=network_state.snapshot,
            provider_request_reservation_status=provider_request_reservation_status,
            runtime_executor_status=runtime_executor_status, super_get_executor_status=super_get_executor_status,
            async_database_pool_status=async_db.pool_status,
            provider_status=provider_status, free_provider_status=free_provider_status,
            realtime_market_session=realtime_market_session, board_curve_session=intraday_board_curve_session,
            scan_interval_seconds=intraday_scan_interval_seconds,
            effective_scan_interval_seconds=intraday_effective_scan_interval_seconds,
            high_frequency_window=intraday_high_frequency_window,
            super_get_fast_interval_seconds=intraday_super_get_fast_interval_seconds,
            super_get_fast_max_in_flight=intraday_super_get_fast_max_in_flight,
            fast_quote_retention_days=intraday_fast_quote_retention_days,
            board_curve_enabled=intraday_board_curve_enabled,
            board_curve_retention_days=intraday_board_curve_retention_days,
            board_rotation_retention_days=intraday_board_rotation_retention_days,
            set_db_pool_gauge=set_db_pool_gauge, set_open_circuit_gauge=provider_circuit_open.set,
            research_storage_governance=local_research_storage_governance,
            background_loop_status=background_loop_registry.snapshot,
        ))
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=503, detail=f"database unavailable: {error}") from error


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Local Prometheus scrape endpoint; service remains loopback-bound."""
    refresh_metrics_control_plane()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)




def intraday_services_status_payload() -> dict[str, Any]:
    """Build the status board through the independent local read model."""
    return read_intraday_services_status_payload(IntradayStatusDependencies(
        database=db, alert_max_attempts=INTRADAY_ALERT_MAX_ATTEMPTS,
        realtime_market_session=realtime_market_session, board_curve_session=intraday_board_curve_session,
        high_frequency_window=intraday_high_frequency_window, scan_interval_seconds=intraday_scan_interval_seconds,
        provider_status=provider_status, runtime_service_state=intraday_runtime_service_state,
        json_safe=strategy_json_safe, super_get_fast_interval_seconds=intraday_super_get_fast_interval_seconds,
        super_get_fast_max_in_flight=intraday_super_get_fast_max_in_flight,
        fast_quote_retention_days=intraday_fast_quote_retention_days, board_curve_enabled=intraday_board_curve_enabled,
        board_curve_retention_days=intraday_board_curve_retention_days,
        board_rotation_retention_days=intraday_board_rotation_retention_days,
        daily_summary_automation_enabled=daily_summary_automation_enabled,
        order_book_max_symbols=intraday_order_book_max_symbols,
    ))


def _intraday_status_dependencies() -> IntradayStatusDependencies:
    return IntradayStatusDependencies(
        database=db, alert_max_attempts=INTRADAY_ALERT_MAX_ATTEMPTS,
        realtime_market_session=realtime_market_session, board_curve_session=intraday_board_curve_session,
        high_frequency_window=intraday_high_frequency_window, scan_interval_seconds=intraday_scan_interval_seconds,
        provider_status=provider_status, runtime_service_state=intraday_runtime_service_state,
        json_safe=strategy_json_safe, super_get_fast_interval_seconds=intraday_super_get_fast_interval_seconds,
        super_get_fast_max_in_flight=intraday_super_get_fast_max_in_flight,
        fast_quote_retention_days=intraday_fast_quote_retention_days, board_curve_enabled=intraday_board_curve_enabled,
        board_curve_retention_days=intraday_board_curve_retention_days,
        board_rotation_retention_days=intraday_board_rotation_retention_days,
        daily_summary_automation_enabled=daily_summary_automation_enabled,
        order_book_max_symbols=intraday_order_book_max_symbols,
    )


async def intraday_services_status_payload_async() -> dict[str, Any]:
    return await read_intraday_services_status_payload_async(
        _intraday_status_dependencies(), async_db,
        realtime_market_session_async, intraday_board_curve_session_async,
    )


app.include_router(build_intraday_status_router(
    intraday_services_status_payload, intraday_services_status_payload_async,
))


@app.post("/api/v1/bootstrap")
def bootstrap() -> dict[str, Any]:
    if not legacy_schema_bootstrap_enabled():
        raise HTTPException(status_code=409, detail="legacy schema bootstrap is disabled; use versioned Alembic migrations")
    db.migrate()
    ensure_catalog_capabilities()
    return {"status": "ok", "catalog": catalog_counts()}


def persist_akshare_probe_result(capability: str, rows: list[dict[str, Any]], symbol: str,
                                 latency_ms: int | None = None) -> int:
    """Persist one bounded AKShare probe result and its health in a DB worker."""
    event_capabilities = {"lhb_event", "strong_pool", "limit_pool"}
    if capability == "daily_bar":
        stored = persist_free_daily("akshare", rows)
    elif capability == "market_summary":
        stored = persist_public_observations("akshare", capability, rows)
    elif capability in event_capabilities:
        bounded = rows[:100] if capability == "lhb_event" else rows[:300]
        stored = persist_market_events("akshare", bounded)
    else:
        stored = persist_public_observations("akshare", capability, rows[:1_000], symbol)
    with db.transaction() as connection:
        record_provider_success(connection, "akshare", capability, stored, latency_ms)
    return stored


def persist_akshare_probe_failure(capability: str, error: str, latency_ms: int | None = None) -> None:
    with db.transaction() as connection:
        record_provider_failure(connection, "akshare", capability, error, latency_ms)


async def akshare_probe(payload: AkShareProbeRequest) -> dict[str, Any]:
    return await run_akshare_probe_isolated(
        payload, today=cn_today, run_akshare=run_akshare_blocking, run_database=run_database_blocking,
        open_provider_capabilities=open_provider_capabilities, persist_result=persist_akshare_probe_result,
        persist_failure=persist_akshare_probe_failure, safe_error_detail=safe_error_detail,
        provider_status=akshare_status,
        sources={
            "daily": akshare_daily, "market_summary": akshare_market_summary, "lhb_events": akshare_lhb_events,
            "strong_pool": akshare_strong_pool_events, "market_breadth": akshare_market_breadth,
            "board_supplements": akshare_board_supplements, "moneyflow_supplements": akshare_moneyflow_supplements,
            "limit_pool_events": akshare_limit_pool_events, "lhb_supplements": akshare_lhb_supplements,
            "block_trade_supplements": akshare_block_trade_supplements,
            "corporate_risk_supplements": akshare_corporate_risk_supplements,
            "analyst_heat_supplements": akshare_analyst_heat_supplements,
            "index_fund_supplements": akshare_index_fund_supplements,
            "macro_cross_asset_supplements": akshare_macro_cross_asset_supplements,
        },
    )


async def probe_realtime_sources(payload: RealtimeProbeRequest) -> dict[str, Any]:
    """Compatibility wrapper for the isolated bounded realtime probe service."""
    return await probe_realtime_sources_isolated(
        payload,
        realtime_probe_matrix=realtime_probe_matrix,
        default_probe_params=default_probe_params,
        realtime_market_session=realtime_market_session_async,
        provider_candidates=provider_candidates,
        fetch=stock_study_fetch,
    )


async def audit_tushare_capabilities(payload: TushareCapabilityAuditRequest) -> dict[str, Any]:
    """Compatibility wrapper for the isolated capability-audit service."""
    async def record_timeout(provider: str, api_name: str) -> None:
        provider_key = f"tushare_{provider}"

        def persist() -> None:
            with db.transaction() as connection:
                record_provider_api_capability(
                    connection, provider_key, api_name, "failed",
                    note="Capability audit timed out after 25 seconds.",
                )

        await run_database_blocking(persist)

    async def load_observation(provider: str, api_name: str) -> dict[str, Any] | None:
        provider_key = f"tushare_{provider}"

        def load() -> Any:
            with db.transaction() as connection:
                return connection.execute(
                    "SELECT availability,note FROM quant.provider_api_capabilities WHERE provider_key=%s AND api_name=%s",
                    (provider_key, api_name),
                ).fetchone()

        observation = await run_database_blocking(load)
        return dict(observation) if observation else None

    return await audit_tushare_capabilities_isolated(
        payload,
        today=cn_today,
        api_capability=api_capability,
        default_probe_params=default_probe_params,
        historical_minute_apis=HISTORICAL_MINUTE_APIS,
        realtime_market_hours_apis=REALTIME_MARKET_HOURS_APIS,
        realtime_market_session=realtime_market_session_async,
        fetch_catalog=fetch_tushare_catalog,
        record_timeout=record_timeout,
        load_observation=load_observation,
        is_local_capacity_error=is_local_capacity_http_error,
        is_circuit_open_error=is_circuit_open_http_error,
    )


async def tushare_fetch(payload: TushareFetchRequest) -> dict[str, Any]:
    return await fetch_tushare_catalog(payload)


async def stock_study(symbol: str, payload: StockStudyRequest | None = None) -> dict[str, Any]:
    """Compatibility service function used by the provider-actions router."""
    return await build_stock_study(symbol, payload or StockStudyRequest())


app.include_router(build_provider_actions_router(ProviderActionDependencies(
    akshare_probe=akshare_probe,
    realtime_probe=probe_realtime_sources,
    tushare_audit=audit_tushare_capabilities,
    tushare_fetch=tushare_fetch,
    stock_study=stock_study,
)))


def tushare_raw(api_name: str, provider: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.tushare_raw(db, api_name, provider, limit, offset, TUSHARE_CATALOG)


def analyse_ingestion(analysis_id: uuid.UUID) -> dict[str, Any]:
    # Compatibility endpoint for older callers.  Analyst claims are now only
    # created from a versioned report read from the remote archive; accepting
    # this call as a no-op prevents a local message from becoming a competing
    # source of investment evidence.
    return {"status": "ignored", "analysis_id": str(analysis_id), "reason": "remote_archive_is_the_only_analyst_source"}


def import_remote_archive_report(payload: RemoteReportImport) -> dict[str, Any]:
    return _remote_archive_actions.import_report(payload)


def import_remote_archive_message(payload: RemoteAnalystMessageImport) -> dict[str, Any]:
    return _remote_archive_actions.import_message(payload)


def reprocess_remote_archive_reports(payload: RemoteReportReprocessRequest) -> dict[str, Any]:
    return _remote_archive_actions.reprocess_reports(payload)


def reprocess_remote_archive_messages(payload: RemoteMessageReprocessRequest) -> dict[str, Any]:
    return _remote_archive_actions.reprocess_messages(payload)


def remote_archive_sync_settings() -> dict[str, Any]:
    return _remote_archive_actions.sync_settings()


async def sync_remote_archive(payload: RemoteArchiveSyncRequest, authorization: str | None = None) -> dict[str, Any]:
    return await _remote_archive_actions.sync(payload, authorization)


def update_analyst_sync_cursor(payload: AnalystSyncCursorUpdate) -> dict[str, Any]:
    return _remote_archive_actions.update_cursor(payload)


def update_analyst_global_sync_cursor(payload: AnalystSyncGlobalCursorUpdate) -> dict[str, Any]:
    return _remote_archive_actions.update_global_cursor(payload)


def update_analyst_research_profile(analyst_id: str, payload: AnalystResearchProfileRequest) -> dict[str, Any]:
    with db.transaction() as connection:
        exists = connection.execute(
            "SELECT 1 FROM quant.remote_analysts WHERE remote_analyst_id=%s", (analyst_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="remote analyst not found")
        connection.execute(
            """INSERT INTO quant.analyst_research_profiles(remote_analyst_id,independence_class,audience_size,audience_as_of,evidence)
               VALUES(%s,%s,%s,%s,%s)
               ON CONFLICT(remote_analyst_id) DO UPDATE SET independence_class=EXCLUDED.independence_class,
                 audience_size=EXCLUDED.audience_size,audience_as_of=EXCLUDED.audience_as_of,evidence=EXCLUDED.evidence,updated_at=now()""",
            (analyst_id, payload.independence_class, payload.audience_size, payload.audience_as_of, payload.evidence),
        )
        result = rebuild_analyst_research(connection, cn_today())
    return {"analyst_id": analyst_id, "status": "updated", "research_status": result["sleeping_experts"]["status"],
            "boundary": "manual provenance prior; no live strategy effect"}


def review_claim_legacy(review_id: uuid.UUID, payload: ClaimReviewRequest) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated claim-review service."""
    return review_claim(review_id, payload)


def review_claim(review_id: uuid.UUID, payload: ClaimReviewRequest) -> dict[str, Any]:
    """Compatibility entry point for point-in-time safe claim review."""
    return review_claim_isolated(review_id, payload, database=db, exchange_for=exchange_for)


def universe_members(universe_key: str) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.universe_members(db, universe_key)


def update_universe_members(payload: UniverseUpdateRequest) -> dict[str, Any]:
    with db.transaction() as connection:
        for symbol in payload.symbols:
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'universe') ON CONFLICT(symbol) DO NOTHING",
                (symbol, exchange_for(symbol)),
            )
            connection.execute(
                """INSERT INTO quant.universe_members(universe_key,symbol,enabled,priority,source,updated_at)
                   VALUES(%s,%s,%s,%s,'api',now()) ON CONFLICT(universe_key,symbol) DO UPDATE SET enabled=EXCLUDED.enabled,
                   priority=EXCLUDED.priority,source=EXCLUDED.source,updated_at=now()""",
                (payload.universe_key, symbol, payload.enabled, payload.priority),
            )
        active = connection.execute(
            "SELECT symbol FROM quant.universe_members WHERE universe_key=%s AND enabled ORDER BY symbol",
            (payload.universe_key,),
        ).fetchall()
        history = sync_universe_membership_history(
            connection, payload.universe_key, cn_today(),
            [str(row["symbol"]) for row in active], source="universe-members-api", priority=payload.priority,
        )
    return {"universe_key": payload.universe_key, "updated": len(payload.symbols), "enabled": payload.enabled,
            "history": history}


def build_features(payload: GenerateRequest) -> dict[str, Any]:
    return build_feature_snapshot(payload.as_of_date or cn_today(), payload.universe_key)


def latest_features(universe_key: str = "core", limit: int = 200) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.latest_features(db, universe_key, limit)


def research_window(connection: Any, universe_key: str, start_date: date | None, end_date: date | None) -> tuple[date, date]:
    bounds = connection.execute(
        """SELECT min(b.trading_date) earliest,max(b.trading_date) latest FROM quant.canonical_bars_daily b
           JOIN quant.universe_membership_history membership ON membership.symbol=b.symbol
            AND membership.universe_key=%s AND membership.effective_from<=b.trading_date
            AND (membership.effective_to IS NULL OR membership.effective_to>=b.trading_date)""",
        (universe_key,),
    ).fetchone()
    if not bounds or not bounds["latest"]:
        raise HTTPException(status_code=422, detail="universe has no canonical daily bars")
    end = min(end_date or bounds["latest"], bounds["latest"])
    start = start_date or max(bounds["earliest"], end - timedelta(days=730))
    if start >= end:
        raise HTTPException(status_code=422, detail="research window must contain at least two dates")
    return start, end


def factor_registry() -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.factor_registry(db)


def evaluate_factors(payload: FactorEvaluationRequest) -> dict[str, Any]:
    with db.transaction() as connection:
        start, end = research_window(connection, payload.universe_key, payload.start_date, payload.end_date)
        rows = connection.execute(
            "SELECT factor_key FROM quant.factor_registry WHERE implementation='native_sql' AND status<>'disabled' ORDER BY factor_key"
        ).fetchall()
        enabled = {str(row["factor_key"]) for row in rows}
        requested = payload.factor_keys or sorted(enabled)
        unknown = sorted(set(requested) - enabled)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown or disabled factors: {', '.join(unknown)}")
        try:
            evaluated = evaluate_factor_set(connection, requested, payload.universe_key, start, end, payload.horizon_days)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        results = []
        for result in evaluated:
            factor_key = str(result["factor_key"])
            row = connection.execute(
                """INSERT INTO quant.factor_evaluations(factor_key,universe_key,start_date,end_date,horizon_days,engine,status,observations,
                    cross_section_days,metrics,artifact) VALUES(%s,%s,%s,%s,%s,'native_factor_sql_v2',%s,%s,%s,%s,%s) RETURNING evaluation_id""",
                (factor_key, payload.universe_key, start, end, payload.horizon_days, result["status"], result["observations"],
                 result["cross_section_days"], Json(result["metrics"]), Json(result["artifact"])),
            ).fetchone()
            result["evaluation_id"] = str(row["evaluation_id"])
            results.append(result)
    return {"universe_key": payload.universe_key, "start_date": str(start), "end_date": str(end), "results": results}


def factor_evaluations(universe_key: str = "core", limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.factor_evaluations(db, universe_key, limit)


def strategy_registry() -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.strategy_registry(db)


def backtest_strategy(payload: StrategyBacktestRequest) -> dict[str, Any]:
    with db.transaction() as connection:
        registry = connection.execute(
            "SELECT strategy_key,configuration FROM quant.strategy_registry WHERE strategy_key=%s AND status<>'disabled'",
            (payload.strategy_key,),
        ).fetchone()
        if not registry:
            raise HTTPException(status_code=404, detail="strategy is not available")
        start, end = research_window(connection, payload.universe_key, payload.start_date, payload.end_date)
        parameters = {**dict(registry["configuration"]), "rebalance_days": payload.rebalance_days, "hold_days": payload.hold_days,
                      "top_n": payload.top_n, "total_cost_bps": payload.total_cost_bps, "factors": payload.factors}
        try:
            result = run_multi_factor_strategy_sql(connection, payload.universe_key, start, end, parameters)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        row = connection.execute(
            """INSERT INTO quant.strategy_experiments(strategy_key,universe_key,start_date,end_date,status,parameters,metrics,equity_curve,trades)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING strategy_experiment_id""",
            (payload.strategy_key, payload.universe_key, start, end, result["status"], Json(result["parameters"]),
             Json(result["metrics"]), Json(result["equity_curve"]), Json(result["trades"])),
        ).fetchone()
    result["strategy_experiment_id"] = str(row["strategy_experiment_id"])
    return result


def strategy_experiments(universe_key: str = "core", limit: int = 50) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.strategy_experiments(db, universe_key, limit)


def reconcile_stale_fetch_runs(payload: FetchRunReconcileRequest) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=payload.max_age_minutes)
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT fetch_run_id,provider_key,capability,trade_date,request_key,status,started_at,created_at
                 FROM quant.fetch_runs
                WHERE status='running' AND coalesce(started_at,created_at)<%s
                ORDER BY coalesce(started_at,created_at)""",
            (cutoff,),
        ).fetchall()
        if not payload.dry_run and rows:
            connection.execute(
                """UPDATE quant.fetch_runs
                      SET status=%s,finished_at=now(),error_class='stale_running_reconciled',
                          error_message='Run exceeded the operational max age and was reconciled by /operations/fetch-runs/reconcile-stale'
                    WHERE status='running' AND coalesce(started_at,created_at)<%s""",
                (payload.terminal_status, cutoff),
            )
    return {"status": "dry_run" if payload.dry_run else "completed", "max_age_minutes": payload.max_age_minutes,
            "terminal_status": payload.terminal_status, "matched": len(rows), "items": rows}


def data_quality_issues(limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.data_quality_issues(db, limit)


def build_snapshot(payload: SnapshotRequest) -> dict[str, Any]:
    as_of = payload.as_of_date or cn_today()
    cutoff = as_utc(payload.knowledge_cutoff) if payload.knowledge_cutoff else datetime.now(timezone.utc)
    with db.transaction() as connection:
        manifest = connection.execute(
            """SELECT (SELECT count(*)::int FROM quant.canonical_bars_daily WHERE trading_date<=%s) bars,
                      (SELECT count(*)::int FROM quant.remote_reports WHERE remote_updated_at<=%s) remote_reports,
                      (SELECT count(*)::int FROM quant.canonical_bars_daily WHERE symbol='000300.SH' AND trading_date<=%s) benchmark_bars,
                      -- Canonical bars also retain benchmark indexes.  Snapshot
                      -- control coverage must be measured against the actual
                      -- A-share code space, otherwise index bars make a fully
                      -- covered stock cross-section look incomplete.
                      (SELECT count(DISTINCT symbol)::int FROM quant.canonical_bars_daily
                        WHERE trading_date=%s
                          AND symbol ~ '^(?:(?:60[0135]|68[0-9])[0-9]{3}\\.SH|(?:000|001|002|003|300|301|302)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$') equity_symbols,
                      (SELECT count(DISTINCT basic.symbol)::int
                         FROM quant.canonical_bars_daily bar
                         JOIN quant.daily_fundamentals basic
                           ON basic.symbol=bar.symbol AND basic.trading_date=bar.trading_date
                        WHERE bar.trading_date=%s
                          AND bar.symbol ~ '^(?:(?:60[0135]|68[0-9])[0-9]{3}\\.SH|(?:000|001|002|003|300|301|302)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$') fundamental_symbols,
                      (SELECT count(DISTINCT limits.symbol)::int
                         FROM quant.canonical_bars_daily bar
                         JOIN quant.daily_trade_limits limits
                           ON limits.symbol=bar.symbol AND limits.trading_date=bar.trading_date
                        WHERE bar.trading_date=%s
                          AND bar.symbol ~ '^(?:(?:60[0135]|68[0-9])[0-9]{3}\\.SH|(?:000|001|002|003|300|301|302)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$') limit_symbols,
                      (SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s) exchange_open,
                      (SELECT count(*)::int FROM quant.data_quality_issues WHERE resolved_at IS NULL AND severity IN ('error','blocking')) blocking_issues""",
            (as_of, cutoff, as_of, as_of, as_of, as_of, as_of),
        ).fetchone()
        # A zero-bar snapshot is a valid operational state but never a valid
        # input to a recommendation run.  Make the absence explicit rather
        # than minting a deceptively "ready" empty snapshot.
        complete_equity_controls = manifest["equity_symbols"] > 0 and manifest["fundamental_symbols"] >= manifest["equity_symbols"] and manifest["limit_symbols"] >= manifest["equity_symbols"]
        status = "ready" if not manifest["blocking_issues"] and manifest["benchmark_bars"] and manifest["exchange_open"] and complete_equity_controls else "blocked"
        snapshot_key = hashlib.sha256(f"{as_of}:{cutoff.isoformat()}:{manifest['bars']}:{manifest['remote_reports']}".encode()).hexdigest()
        connection.execute(
            """INSERT INTO quant.data_snapshots(snapshot_key,as_of_date,knowledge_cutoff,status,manifest,content_sha256,finalized_at)
               VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s='ready' THEN now() ELSE null END)
               ON CONFLICT(snapshot_key) DO NOTHING""",
            (snapshot_key, as_of, cutoff, status, Json(manifest), snapshot_key, status),
        )
    return {"snapshot_key": snapshot_key, "as_of_date": str(as_of), "knowledge_cutoff": cutoff, "status": status, "manifest": manifest}


async def analyse_ingestion_endpoint(analysis_id: uuid.UUID) -> dict[str, Any]:
    # Kept async so the router has one uniform dependency contract, although
    # this legacy compatibility response is intentionally a local no-op.
    return analyse_ingestion(analysis_id)


async def import_remote_archive_report_endpoint(payload: RemoteReportImport) -> dict[str, Any]:
    return await run_database_blocking(import_remote_archive_report, payload, timeout_seconds=30)


async def import_remote_archive_message_endpoint(payload: RemoteAnalystMessageImport) -> dict[str, Any]:
    return await run_database_blocking(import_remote_archive_message, payload, timeout_seconds=30)


async def reprocess_remote_archive_reports_endpoint(payload: RemoteReportReprocessRequest) -> dict[str, Any]:
    return await run_database_blocking(reprocess_remote_archive_reports, payload, timeout_seconds=60)


async def reprocess_remote_archive_messages_endpoint(payload: RemoteMessageReprocessRequest) -> dict[str, Any]:
    return await run_database_blocking(reprocess_remote_archive_messages, payload, timeout_seconds=60)


async def review_claim_endpoint(review_id: uuid.UUID, payload: ClaimReviewRequest) -> dict[str, Any]:
    return await run_database_blocking(review_claim, review_id, payload, timeout_seconds=30)


async def update_universe_members_endpoint(payload: UniverseUpdateRequest) -> dict[str, Any]:
    return await run_database_blocking(update_universe_members, payload, timeout_seconds=30)


async def build_features_endpoint(payload: GenerateRequest) -> dict[str, Any]:
    return await run_database_blocking(build_features, payload, timeout_seconds=60)


async def evaluate_factors_endpoint(payload: FactorEvaluationRequest) -> dict[str, Any]:
    run_key = "factor-evaluate:{universe}:{start}:{end}:{horizon}".format(
        universe=payload.universe_key, start=payload.start_date or "auto",
        end=payload.end_date or "auto", horizon=payload.horizon_days,
    )
    return await run_database_blocking(functools.partial(
        run_recorded, db, task_key="factor_evaluation", run_key=run_key,
        operation=functools.partial(evaluate_factors, payload), cadence="manual",
        methodology_version="native_factor_sql_v2",
        input_summary={"universe_key": payload.universe_key, "horizon_days": payload.horizon_days},
    ), timeout_seconds=300)


async def backtest_strategy_endpoint(payload: StrategyBacktestRequest) -> dict[str, Any]:
    return await run_database_blocking(backtest_strategy, payload, timeout_seconds=300)


async def reconcile_stale_fetch_runs_endpoint(payload: FetchRunReconcileRequest) -> dict[str, Any]:
    return await run_database_blocking(reconcile_stale_fetch_runs, payload, timeout_seconds=30)


async def build_snapshot_endpoint(payload: SnapshotRequest) -> dict[str, Any]:
    return await run_database_blocking(build_snapshot, payload, timeout_seconds=30)


async def update_analyst_research_profile_endpoint(analyst_id: str, payload: AnalystResearchProfileRequest) -> dict[str, Any]:
    return await run_database_blocking(update_analyst_research_profile, analyst_id, payload, timeout_seconds=30)


async def update_analyst_sync_cursor_endpoint(payload: AnalystSyncCursorUpdate) -> dict[str, Any]:
    return await run_database_blocking(update_analyst_sync_cursor, payload, timeout_seconds=30)


async def update_analyst_global_sync_cursor_endpoint(payload: AnalystSyncGlobalCursorUpdate) -> dict[str, Any]:
    return await run_database_blocking(update_analyst_global_sync_cursor, payload, timeout_seconds=30)


async def sync_remote_archive_endpoint(payload: RemoteArchiveSyncRequest, authorization: str | None) -> dict[str, Any]:
    return await sync_remote_archive(payload, authorization)


def replay_recorded_intraday_events(payload: IntradayEventReplayRequest) -> dict[str, Any]:
    with db.transaction() as connection:
        return run_recorded_signal_lifecycle_replay(
            connection, as_of_date=payload.as_of_date, max_events=payload.max_events,
        )


async def replay_recorded_intraday_events_endpoint(payload: IntradayEventReplayRequest) -> dict[str, Any]:
    return await run_database_blocking(replay_recorded_intraday_events, payload, timeout_seconds=60)


def replay_recorded_intraday_rule_inputs(payload: IntradayRuleInputReplayRequest) -> dict[str, Any]:
    def evaluate(inputs: dict[str, Any]) -> list[dict[str, Any]]:
        return intraday_signal_rules(
            inputs["watch"], inputs["quote"], inputs["previous_quote"], inputs["daily_factors"],
            inputs["minute_features"], inputs["peer_context"],
        )

    def evaluate_policy(signal: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        """Replay the same pure risk/policy gate from snapshot-local inputs.

        V1 snapshots never call this function because they did not capture the
        required point-in-time market and portfolio values.  The generic
        replay runner labels them core-rule-only instead of reading current
        database state.
        """
        portfolio_context = dict(inputs.get("portfolio_context") or {})
        portfolio_gate = paper_risk_gate(
            signal_type=str(signal.get("signal_type") or "watch"),
            symbol=str(inputs["watch"]["symbol"]),
            position=dict(portfolio_context.get("position") or {}),
            snapshot=dict(portfolio_context.get("snapshot") or {}),
            candidate_sector_keys=list(portfolio_context.get("candidate_sector_keys") or ()),
        )
        portfolio_risk = {
            "allowed": portfolio_gate.allowed, "target_weight": portfolio_gate.target_weight,
            "reasons": list(portfolio_gate.reasons), "risk_flags": list(portfolio_gate.risk_flags),
        }
        return live_policy_gate(
            signal, inputs["watch"], inputs["quote"], inputs["daily_factors"],
            dict(inputs.get("market_context") or {}), dict(inputs.get("fast_confirmation") or {}),
            portfolio_risk,
        )

    with db.transaction() as connection:
        return run_recorded_rule_input_replay(
            connection, as_of_date=payload.as_of_date, max_rows=payload.max_rows,
            model_version=INTRADAY_SIGNAL_MODEL_VERSION, evaluate=evaluate, evaluate_policy=evaluate_policy,
        )


async def replay_recorded_intraday_rule_inputs_endpoint(payload: IntradayRuleInputReplayRequest) -> dict[str, Any]:
    return await run_database_blocking(replay_recorded_intraday_rule_inputs, payload, timeout_seconds=60)


app.include_router(build_research_actions_router(ResearchActionDependencies(
    analyse_ingestion=analyse_ingestion_endpoint,
    import_remote_report=import_remote_archive_report_endpoint,
    import_remote_message=import_remote_archive_message_endpoint,
    reprocess_remote_reports=reprocess_remote_archive_reports_endpoint,
    reprocess_remote_messages=reprocess_remote_archive_messages_endpoint,
    review_claim=review_claim_endpoint,
    update_universe=update_universe_members_endpoint,
    build_features=build_features_endpoint,
    evaluate_factors=evaluate_factors_endpoint,
    backtest=backtest_strategy_endpoint,
    reconcile_fetch_runs=reconcile_stale_fetch_runs_endpoint,
    build_snapshot=build_snapshot_endpoint,
    update_analyst_research_profile=update_analyst_research_profile_endpoint,
    update_analyst_sync_cursor=update_analyst_sync_cursor_endpoint,
    update_analyst_global_sync_cursor=update_analyst_global_sync_cursor_endpoint,
    sync_remote_archive=sync_remote_archive_endpoint,
    replay_recorded_intraday_events=replay_recorded_intraday_events_endpoint,
    replay_recorded_rule_inputs=replay_recorded_intraday_rule_inputs_endpoint,
)))


def research_overview() -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.research_overview(
        db, current_data_coverage_fn=current_data_coverage, feature_readiness_fn=feature_readiness_state,
        history_estimate_fn=lambda: historical_estimate_from_db(HistoricalCoverageEstimateRequest(years=3, include_minute=False)),
    )


def import_bars(payload: BarsImport) -> dict[str, int]:
    with db.transaction() as connection:
        for bar in payload.bars:
            upsert_bar(connection, bar)
    return {"imported": len(payload.bars)}


async def sync_market_universe_endpoint(payload: MarketUniverseSyncRequest) -> dict[str, Any]:
    return await sync_market_universe(payload)


async def sync_full_market_daily_endpoint(payload: FullMarketDailySyncRequest) -> dict[str, Any]:
    return await sync_full_market_daily(payload)


async def post_close_refresh_endpoint(payload: PostCloseRefreshRequest) -> dict[str, Any]:
    return await run_post_close_refresh(payload)


async def sync_cninfo_events_endpoint(payload: AnnouncementSyncRequest) -> dict[str, Any]:
    return await sync_cninfo_announcements(payload)


async def rebuild_market_flow_features_endpoint(payload: MarketFlowFeatureRebuildRequest) -> dict[str, Any]:
    return await run_database_blocking(
        rebuild_stored_market_flow_features,
        db,
        payload.start_date,
        payload.end_date,
        timeout_seconds=90,
    )


app.include_router(build_market_actions_router(MarketActionDependencies(
    import_bars=import_bars,
    sync_universe=sync_market_universe_endpoint,
    sync_full_daily=sync_full_market_daily_endpoint,
    post_close_refresh=post_close_refresh_endpoint,
    sync_announcements=sync_cninfo_events_endpoint,
    rebuild_market_flow_features=rebuild_market_flow_features_endpoint,
)))


async def sync_sector_catalog_endpoint(payload: SectorCatalogSyncRequest) -> dict[str, Any]:
    return await sync_all_ths_sector_catalogs() if payload.all_types else await sync_ths_sector_catalog(payload)


async def sync_eastmoney_sector_members_endpoint(payload: EastmoneyBoardMemberSyncRequest) -> dict[str, Any]:
    return await sync_eastmoney_board_members(payload)


async def intraday_sector_report_endpoint(payload: IntradaySectorReportRequest) -> dict[str, Any]:
    report = await intraday_sector_report(payload)
    report.pop("_runtime_quotes", None)
    return report


async def run_strategy_decision_endpoint(payload: StrategyDecisionRequest) -> dict[str, Any]:
    return await run_strategy_decision(payload)


async def run_strategy_review(payload: StrategyReviewRequest) -> dict[str, Any]:
    """Materialize a noon/close review without fetching or downloading media."""
    def persist_review() -> dict[str, Any]:
        with db.transaction() as connection:
            return strategy_review_payload(connection, payload)
    return await run_database_blocking(persist_review, timeout_seconds=30)


async def run_post_close_strategy_endpoint(payload: PostCloseStrategyRequest) -> dict[str, Any]:
    return await run_database_blocking(run_post_close_strategy, payload, timeout_seconds=60)


async def run_strategy_pattern_mining_endpoint(payload: StrategyPatternMiningRequest) -> dict[str, Any]:
    return await run_strategy_pattern_mining(payload)


def persist_watchlist_main_wave_research(payload: WatchlistMainWaveResearchRequest) -> dict[str, Any]:
    """Fit and persist breakout plus counter-trend watchlist shadow models."""
    with db.transaction() as connection:
        result = run_watchlist_main_wave_v2_research(connection, payload.as_of_date)
        rebound_result = run_countertrend_rebound_research(connection, payload.as_of_date)
        persisted = {}
        for model_result in (result, rebound_result):
            row = connection.execute(
                """INSERT INTO quant.strategy_experiments(
                       strategy_key,universe_key,start_date,end_date,status,parameters,metrics,equity_curve,trades)
                   VALUES(%s,'watchlist',%s,%s,%s,%s,%s,%s,%s)
                   RETURNING strategy_experiment_id,created_at""",
                (model_result["strategy_key"],
                 model_result.get("start_date") or payload.as_of_date or cn_today(),
                 model_result.get("end_date") or payload.as_of_date or cn_today(), model_result["status"],
                 Json(strategy_json_safe(model_result.get("parameters", {}))),
                 Json(strategy_json_safe(model_result.get("metrics", {}))),
                 Json(strategy_json_safe(model_result.get("equity_curve", []))),
                 Json(strategy_json_safe(model_result.get("trades", [])))),
            ).fetchone()
            persisted[model_result["strategy_key"]] = {
                **model_result, "strategy_experiment_id": str(row["strategy_experiment_id"]),
                "created_at": row["created_at"],
            }
    return {
        **persisted[WATCHLIST_MAIN_WAVE_STRATEGY_KEY],
        "countertrend_rebound": persisted[WATCHLIST_REBOUND_STRATEGY_KEY],
    }


async def run_watchlist_main_wave_endpoint(payload: WatchlistMainWaveResearchRequest) -> dict[str, Any]:
    return await run_database_blocking(persist_watchlist_main_wave_research, payload, timeout_seconds=90)


def latest_strategy_pattern_mining() -> dict[str, Any]:
    """Compatibility export; HTTP reads use the isolated read model."""
    return read_latest_strategy_pattern_mining(
        db, merge_limit_pool_sources, limit_board_count, strategy_json_safe,
        post_close_limit_daily_features, post_close_exact_board_context,
        post_close_tushare_lhb_context,
    )


def list_intraday_watchlists() -> dict[str, Any]:
    """Compatibility export for the intraday-evidence read model."""
    return intraday_evidence_reads.watchlists(db)


def latest_intraday_decision_card(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise HTTPException(status_code=422, detail="symbol must use the Tushare form, for example 600176.SH")
    return intraday_evidence_reads.decision_card(db, symbol, intraday_decision_card)


async def upsert_intraday_watchlist(symbol: str, payload: IntradayWatchlistRequest) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol != payload.symbol.upper():
        raise HTTPException(status_code=422, detail="path symbol must match payload symbol")
    def persist_watchlist() -> Any:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.instruments(symbol,exchange,name,source) VALUES(%s,%s,%s,'intraday_watchlist')
                   ON CONFLICT(symbol) DO NOTHING""",
                (symbol, exchange_for(symbol), payload.label),
            )
            return connection.execute(
                """INSERT INTO quant.intraday_watchlists(symbol,label,enabled,alert_on_entry,alert_on_exit,entry_price,available_quantity,hard_stop,take_profit,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(symbol) DO UPDATE SET label=EXCLUDED.label,enabled=EXCLUDED.enabled,alert_on_entry=EXCLUDED.alert_on_entry,
                      alert_on_exit=EXCLUDED.alert_on_exit,entry_price=EXCLUDED.entry_price,available_quantity=EXCLUDED.available_quantity,
                      hard_stop=EXCLUDED.hard_stop,take_profit=EXCLUDED.take_profit,metadata=EXCLUDED.metadata,updated_at=now()
                   RETURNING *""",
                (symbol, payload.label, payload.enabled, payload.alert_on_entry, payload.alert_on_exit, payload.entry_price,
                 payload.available_quantity, payload.hard_stop, payload.take_profit, Json(payload.metadata)),
            ).fetchone()

    row = await run_database_blocking(persist_watchlist)
    history = await hydrate_watchlist_history(row["watchlist_id"], symbol)
    return {"item": row, "history_hydration": history, "notice": "已更新提醒范围并拉取了受限历史；不构成交易指令，也不会自动下单。"}


async def sync_intraday_watchlist_history(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    def load_watch() -> Any:
        with db.transaction() as connection:
            return connection.execute("SELECT watchlist_id,symbol FROM quant.intraday_watchlists WHERE symbol=%s", (symbol,)).fetchone()

    watch = await run_database_blocking(load_watch)
    if not watch:
        raise HTTPException(status_code=404, detail="watchlist symbol not found")
    return await hydrate_watchlist_history(watch["watchlist_id"], symbol)


async def delete_intraday_watchlist(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    def delete_watchlist() -> Any:
        with db.transaction() as connection:
            return connection.execute(
                "DELETE FROM quant.intraday_watchlists WHERE symbol=%s RETURNING watchlist_id", (symbol,)
            ).fetchone()

    row = await run_database_blocking(delete_watchlist)
    if not row:
        raise HTTPException(status_code=404, detail="watchlist symbol not found")
    return {"status": "deleted", "symbol": symbol}


async def run_intraday_watchlist_scan_endpoint(payload: IntradayScanRequest) -> dict[str, Any]:
    return await run_intraday_watchlist_scan(payload)


async def capture_intraday_minute_sessions_endpoint(payload: MinuteSessionCaptureRequest) -> dict[str, Any]:
    """Manually run the same bounded in-session baseline capture as the scheduler."""
    symbols = payload.symbols
    if not symbols:
        def load_enabled_watches() -> list[Any]:
            with db.transaction() as connection:
                return connection.execute(
                    "SELECT * FROM quant.intraday_watchlists WHERE enabled ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
                    (intraday_minute_profile_max_symbols(),),
                ).fetchall()

        rows = await run_database_blocking(load_enabled_watches)
        symbols = [str(row["symbol"]) for row in sorted((dict(row) for row in rows), key=intraday_watch_priority_key)]
    return await capture_intraday_minute_sessions(symbols)


async def run_intraday_board_report_endpoint() -> dict[str, Any]:
    return await run_intraday_board_report()


async def run_close_sector_review_report_endpoint() -> dict[str, Any]:
    """Persist a post-close board report without sending a duplicate chat alert."""
    return await run_intraday_board_report(deliver=False)


app.include_router(build_intraday_actions_router(IntradayActionDependencies(
    upsert_watchlist=upsert_intraday_watchlist,
    sync_watchlist_history=sync_intraday_watchlist_history,
    delete_watchlist=delete_intraday_watchlist,
    scan_watchlist=run_intraday_watchlist_scan_endpoint,
    capture_minute_sessions=capture_intraday_minute_sessions_endpoint,
    board_report=run_intraday_board_report_endpoint,
    close_board_report=run_close_sector_review_report_endpoint,
)))


def latest_close_sector_review_report() -> dict[str, Any]:
    """Compatibility export for the local board-review read model."""
    return read_latest_close_sector_review_report(db)


def intraday_board_flow_curves(
    trade_date: date | None = None,
    taxonomy: Literal["industry", "concept"] = "industry",
    since: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility export for the bounded board-curve read model."""
    return read_intraday_board_flow_curves(
        db, trade_date, taxonomy, since,
        curve_retention_days=intraday_board_curve_retention_days(),
        rotation_retention_days=intraday_board_rotation_retention_days(),
    )


def ths_concept_member_backfill_status(trade_date: date | None = None) -> dict[str, Any]:
    """Compatibility export for the sector read model."""
    return sector_reads.concept_member_backfill_status(
        db, trade_date,
        automatic_enabled=ths_concept_member_backfill_enabled(), batch_size=ths_concept_member_backfill_batch_size(),
    )


def latest_intraday_watchlist_scan() -> dict[str, Any]:
    """Compatibility export for the bounded intraday-evidence read model."""
    return intraday_evidence_reads.latest_scan(db)


async def sync_sector_flows_endpoint(payload: SectorFlowSyncRequest) -> dict[str, Any]:
    return await sync_ths_industry_moneyflow(payload)


async def sync_sector_concepts_endpoint(payload: SectorFlowSyncRequest) -> dict[str, Any]:
    return await sync_ths_concept_signals(payload)


async def sync_sector_concept_members_endpoint(payload: ConceptMemberSyncRequest) -> dict[str, Any]:
    return await sync_ths_concept_members(payload)


async def backfill_sector_concept_members_endpoint(payload: ConceptMemberBackfillRequest) -> dict[str, Any]:
    """Run exactly one resumable THS concept-member batch; never scrape by name."""
    return await run_ths_concept_member_backfill_batch(payload)


async def backfill_all_sector_members_endpoint(payload: AllBoardMemberBackfillRequest) -> dict[str, Any]:
    """Advance one cross-source member-mapping batch with durable progress."""
    return await run_all_board_member_backfill_batch(payload)


async def sync_concept_candidates_endpoint(payload: ConceptCandidateSyncRequest) -> dict[str, Any]:
    return await sync_concept_limit_candidates(payload)


async def run_concept_board_research_endpoint(payload: BoardResearchRunRequest) -> dict[str, Any]:
    return await run_board_research(payload)


app.include_router(build_sector_actions_router(SectorActionDependencies(
    sync_catalog=sync_sector_catalog_endpoint,
    sync_eastmoney_members=sync_eastmoney_sector_members_endpoint,
    intraday_report=intraday_sector_report_endpoint,
    sync_industry_flows=sync_sector_flows_endpoint,
    sync_concepts=sync_sector_concepts_endpoint,
    sync_concept_members=sync_sector_concept_members_endpoint,
    backfill_concept_members=backfill_sector_concept_members_endpoint,
    backfill_all_members=backfill_all_sector_members_endpoint,
    sync_concept_candidates=sync_concept_candidates_endpoint,
    run_board_research=run_concept_board_research_endpoint,
)))


def concept_sector_signals(trade_date: date | None = None, limit: int = 500) -> dict[str, Any]:
    """Compatibility export for the sector read model."""
    return sector_reads.concept_sector_signals(db, trade_date, limit)


def concept_limit_candidates(trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the sector read model."""
    return sector_reads.concept_limit_candidates(db, trade_date, limit)


def sector_flows(taxonomy_key: str = "ths_industry", trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the sector read model."""
    return sector_reads.sector_flows(db, taxonomy_key, trade_date, limit)


def market_sectors(taxonomy_key: str = "ths_index_n", limit: int = 500, offset: int = 0) -> dict[str, Any]:
    """Compatibility export for the sector read model."""
    return sector_reads.market_sectors(db, taxonomy_key, limit, offset)


def sector_members(sector_key: str, taxonomy_key: str = "ths_index_n", limit: int = 500, offset: int = 0) -> dict[str, Any]:
    """Compatibility export for the sector read model."""
    return sector_reads.sector_members(db, sector_key, taxonomy_key, limit, offset)


async def run_market_snapshot_endpoint(payload: MarketSnapshotRequest) -> dict[str, Any]:
    return await build_market_snapshot(payload)


def market_snapshots(limit: int = 20) -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.market_snapshots(db, limit)


def import_offline_minute_bars(payload: OfflineMinuteImportRequest) -> dict[str, Any]:
    try:
        return import_offline_minute_csv(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def import_offline_minute_bars_endpoint(payload: OfflineMinuteImportRequest) -> dict[str, Any]:
    return await run_database_blocking(import_offline_minute_bars, payload, timeout_seconds=60)


def offline_minute_imports(limit: int = 30) -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.offline_minute_imports(db, limit, str(offline_data_root()))


async def sync_tushare_endpoint(payload: TushareSyncRequest) -> dict[str, Any]:
    return await sync_tushare(payload)


async def sync_baostock_endpoint(payload: TushareSyncRequest) -> dict[str, Any]:
    return await sync_baostock(payload)


async def sync_tushare_core_endpoint(payload: TushareSyncRequest) -> dict[str, Any]:
    return await sync_tushare_daily_core(payload.trade_date or payload.end_date or cn_today(), payload.symbols)


app.include_router(build_ingestion_actions_router(IngestionActionDependencies(
    market_snapshot=run_market_snapshot_endpoint,
    import_offline_minutes=import_offline_minute_bars_endpoint,
    sync_tushare=sync_tushare_endpoint,
    sync_baostock=sync_baostock_endpoint,
    sync_tushare_core=sync_tushare_core_endpoint,
)))


async def scorecards(as_of_date: date | None = None) -> dict[str, Any]:
    return await run_database_blocking(recompute_scorecards, as_of_date, timeout_seconds=30)


def analyst_scorecards(limit: int = 200) -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.analyst_scorecards(db, limit, analyst_scorecard_readiness)


async def outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    return await run_database_blocking(recompute_outcomes, as_of_date, timeout_seconds=60)


async def intraday_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    return await run_database_blocking(recompute_intraday_signal_outcomes, as_of_date, timeout_seconds=60)


def latest_intraday_outcomes(limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the bounded intraday-outcome read model."""
    return read_latest_intraday_outcomes(
        db, limit,
        market_context_batch_fn=intraday_point_in_time_market_context_batch,
        attribution_fn=intraday_signal_attribution,
        attribution_summary_fn=intraday_outcome_attribution_summary,
    )


async def recommendations(payload: GenerateRequest) -> dict[str, Any]:
    return await run_database_blocking(generate_recommendations, payload, timeout_seconds=30)


async def run_daily_pipeline(payload: GenerateRequest) -> dict[str, Any]:
    return await run_daily_pipeline_orchestrated(
        payload, sync_tushare=sync_tushare, sync_baostock=sync_baostock,
        sync_tushare_daily_core=sync_tushare_daily_core, tushare_request=TushareSyncRequest,
        snapshot_request=lambda as_of: SnapshotRequest(as_of_date=as_of), build_snapshot=build_snapshot,
        recompute_outcomes=recompute_outcomes, recompute_scorecards=recompute_scorecards,
        generate_recommendations=generate_recommendations, run_database_blocking=run_database_blocking,
        cn_today=cn_today,
    )


app.include_router(build_strategy_actions_router(StrategyActionDependencies(
    decision=run_strategy_decision_endpoint,
    review=run_strategy_review,
    post_close=run_post_close_strategy_endpoint,
    pattern_mining=run_strategy_pattern_mining_endpoint,
    watchlist_main_wave=run_watchlist_main_wave_endpoint,
    recompute_scorecards=scorecards,
    recompute_outcomes=outcomes,
    recompute_intraday_outcomes=intraday_outcomes,
    generate_recommendations=recommendations,
    daily_pipeline=run_daily_pipeline,
)))


def latest_recommendations() -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.latest_recommendations(db)


def metrics() -> dict[str, Any]:
    """Compatibility export for the market-result read model."""
    return market_result_reads.metrics(db)

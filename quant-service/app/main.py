from __future__ import annotations
import asyncio
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
from .analysis import as_utc
from .capability_registry import api_capability
from .database import AsyncDatabase, Database
from .async_provider_circuit_repository import open_capabilities as read_async_open_provider_capabilities
from .async_provider_circuit_repository import open_provider_keys as read_async_open_provider_keys
from .async_market_session_repository import realtime_market_session as read_async_realtime_market_session
from .async_market_session_repository import sse_calendar_open as read_async_sse_calendar_open
from .async_market_session_repository import sse_calendar_status as read_async_sse_calendar_status
from .daily_bar_repository import exchange_for, provider_priority, upsert_daily_bar
from .public_market_repository import (
    persist_free_daily as _persist_free_daily,
    persist_free_quote as _persist_free_quote,
    persist_free_quotes as _persist_free_quotes,
    persist_market_events as _persist_market_events,
    persist_public_observations as _persist_public_observations,
    recent_market_events as _recent_market_events,
)
from .factor_sql_lab import evaluate_factor_set, run_multi_factor_strategy_sql
from .research_experiment_service import (
    ResearchExperimentDependencies,
    backtest_strategy as backtest_strategy_isolated,
    build_snapshot as build_snapshot_isolated,
    evaluate_factors as evaluate_factors_isolated,
    research_window as research_window_isolated,
)
from .research_maintenance_service import (
    ResearchMaintenanceDependencies,
    reconcile_stale_fetch_runs as reconcile_stale_fetch_runs_isolated,
    update_analyst_profile as update_analyst_research_profile_isolated,
    update_universe_members as update_universe_members_isolated,
)
from .intraday_watchlist_service import (
    IntradayWatchlistDependencies,
    delete as delete_intraday_watchlist_isolated,
    sync_history as sync_intraday_watchlist_history_isolated,
    upsert as upsert_intraday_watchlist_isolated,
)
from .tushare_fetch_ledger import (
    TushareFetchLedgerDependencies,
    persist_blocked as persist_tushare_fetch_blocked_isolated,
    persist_cancel as persist_tushare_fetch_cancel_isolated,
    persist_failure as persist_tushare_fetch_failure_isolated,
    persist_success as persist_tushare_fetch_success_isolated,
    prepare_run as prepare_tushare_fetch_run_isolated,
)
from .analyst_promotion import MAX_APPROVED_WEIGHT, PROMOTION_KEY, analyst_live_promotion
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
from .intraday_quote_normalization import (
    annotate_flow_percentiles as annotate_intraday_flow_percentiles_pure,
    exchange_time_status as intraday_quote_exchange_time_status_pure,
    merge_eastmoney_watch_flows as merge_intraday_eastmoney_watch_flows_pure,
    merge_sina_watch_quotes as merge_intraday_sina_watch_quotes_pure,
    merge_watch_quote_prices as merge_intraday_watch_quote_prices_pure,
    observation_source as intraday_quote_observation_source_pure,
    quote_from_tencent as intraday_quote_from_tencent_pure,
)
from .intraday_decision_card_read_model import decision_card as read_intraday_decision_card
from .async_intraday_decision_card_repository import decision_card as read_async_intraday_decision_card
from .async_intraday_scan_preflight_repository import latest_board_report as read_async_latest_board_report
from .async_intraday_scan_preflight_repository import latest_fast_quotes as read_async_latest_fast_quotes
from .async_intraday_scan_inputs_repository import exact_memberships as read_async_exact_watchlist_memberships
from .async_intraday_scan_inputs_repository import watchlists as read_async_intraday_scan_watchlists
from .async_ths_concept_member_backfill_repository import existing_flow_rows as read_async_ths_concept_flow_rows
from .async_ths_concept_member_backfill_repository import member_progress as read_async_ths_concept_member_progress
from .async_sync_symbol_repository import analyst_claim_symbols as read_async_analyst_claim_symbols
from .async_sync_symbol_repository import core_symbols as read_async_core_symbols
from .async_sync_symbol_repository import limited_core_symbols as read_async_limited_core_symbols
from .async_runtime_lease_repository import acquire as acquire_background_runtime_lease
from .async_runtime_lease_repository import release as release_background_runtime_lease
from .async_runtime_lease_repository import renew as renew_background_runtime_lease
from .intraday_volume_profiles import attach_volume_time_profile as pure_attach_volume_time_profile
from .intraday_volume_profiles import volume_time_profile as pure_intraday_volume_time_profile
from .intraday_volume_profiles import volume_time_profiles as pure_intraday_volume_time_profiles
from .intraday_minute_provider_service import fetch_bounded_minute_context
from .intraday_surge_context_service import capture as capture_intraday_surge_context
from .strategy_candidate_ranking import select as select_intraday_candidates
from . import offline_minute_import_service
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
from .limit_pool_merge import merge_limit_pool_sources as merge_persisted_limit_pool_sources
from .strategy_pattern_sample_repository import (
    load_strategy_pattern_sample_inputs,
    persist_strategy_pattern_run as persist_strategy_pattern_run_isolated,
)
from .strategy_pattern_mining_service import (
    StrategyPatternMiningDependencies,
    run_strategy_pattern_mining as run_strategy_pattern_mining_isolated,
)
from .post_close_strategy_service import (
    candidates as persisted_post_close_strategy_candidates,
    completed_for_date as persisted_post_close_strategy_completed_for_date,
    retry_window as post_close_strategy_retry_window,
    run as persisted_run_post_close_strategy,
)
from .post_close_scheduler import PostCloseSchedulerDependencies, post_close_strategy_scheduler
from .strategy_review_scheduler import StrategyReviewSchedulerDependencies, strategy_review_scheduler
from .strategy_runtime_runners import (
    PostCloseStrategyRuntimeDependencies,
    StrategyReviewRuntimeDependencies,
    run_post_close_strategy_loop as run_post_close_strategy_runtime_loop,
    run_strategy_review_loop as run_strategy_review_runtime_loop,
)
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
from .order_book_features import aggregate_order_book_observations
from . import intraday_order_book_service as order_book_service
from . import intraday_order_book_runner
from .intraday_order_book_runtime import (
    IntradayOrderBookRuntimeDependencies,
    run_intraday_order_book_runtime_loop,
)
from . import intraday_minute_profile_runner
from .intraday_minute_profile_runtime import (
    IntradayMinuteProfileRuntimeDependencies,
    run_intraday_minute_profile_runtime_loop,
)
from . import intraday_board_curve_runner
from .intraday_board_curve_runtime import (
    IntradayBoardCurveRuntimeDependencies,
    run_intraday_board_curve_runtime_loop,
)
from . import intraday_fast_quote_capture_service
from .market_snapshots import snapshot_status, summarize_quotes
from .market_flow_repository import (
    persist_intraday_market_flow_feature,
    persist_market_snapshot_flow_feature,
    rebuild_stored_market_flow_features,
)
from .intraday_alerts import daily_strategy_summary_text, delivery_health_recovery_text, intraday_alert_text
from . import intraday_alert_delivery_service
from . import intraday_board_report_service
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
from .intraday_fast_quote_runtime import (
    IntradayFastQuoteRuntimeDependencies,
    run_intraday_fast_quote_runtime_loop,
)
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
from .runtime_tasks import (
    BackgroundTaskSpec, LoopRuntimeRegistry, cancel_background_tasks,
    observe_completed_task, start_leased_background_tasks, supervise_leased_loop, supervise_loop,
)
from .application_lifecycle import ApplicationLifecycleDependencies, application_lifespan
from .intraday_outcomes import (
    INTRADAY_OUTCOME_HORIZONS,
    intraday_outcome_cutoff,
    intraday_signal_direction,
    intraday_signal_outcome_metrics,
    a_share_return_decomposition,
)
from .intraday_scan_repository import (
    first_eac_breakout_events,
    load_intraday_scan_local_state,
    load_intraday_signal_event_state,
    persist_intraday_scan_terminal,
    previous_quote_frames,
)
from .intraday_market_context_repository import (
    market_context_from_board_report as read_market_context_from_board_report,
    point_in_time_market_context as read_point_in_time_market_context,
    point_in_time_market_context_batch as read_point_in_time_market_context_batch,
)
from .intraday_rule_snapshot_repository import persist_rule_input_snapshot, prune_rule_input_evidence
from .intraday_event_retention import ephemeral_signal_retention_days, prune_ephemeral_signal_events
from .market_session_repository import (
    realtime_market_session as read_realtime_market_session,
    realtime_market_session_async as read_realtime_market_session_async,
    sse_calendar_open as read_sse_calendar_open,
    sse_calendar_open_async as read_sse_calendar_open_async,
    sse_calendar_status as read_sse_calendar_status,
    sse_calendar_status_async as read_sse_calendar_status_async,
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
    runtime_resource_status,
)
from .research_storage_admission import ResearchStorageAdmission, governance as research_storage_governance_isolated
from .health_read_model import DatabaseUnavailableError, HealthDependencies, health_payload as read_health_payload
from .replay_readiness import historical_replay_readiness
from . import research_capacity
from .feature_read_repository import analyst_feature as read_analyst_feature
from .feature_read_repository import latest_tushare_row as read_latest_tushare_row
from .feature_read_repository import market_regime as read_market_regime
from .analyst_text_features import DEFAULT_FACTOR_VERSION, analyst_text_factor_summary as read_analyst_text_factor_summary
from .stock_study_readiness_repository import (
    raw_api_window_summary as read_raw_api_window_summary,
    stock_study_claims as read_stock_study_claims,
    stock_window_readiness as read_stock_window_readiness,
)
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
from .daily_strategy_summary_scheduler import daily_strategy_summary_scheduler
from .daily_strategy_summary_runtime import (
    DailyStrategySummaryRuntimeDependencies,
    run_daily_strategy_summary as run_daily_strategy_summary_runtime,
    run_daily_strategy_summary_loop as run_daily_strategy_summary_runtime_loop,
)
from .strategy_decision_service import run as run_strategy_decision_isolated
from .strategy_review_service import build as build_strategy_review_isolated, completed_for_checkpoint as review_checkpoint_completed_isolated
from .strategy_context_read_model import (
    event_context as read_strategy_event_context,
    index_breadth_context as read_strategy_index_breadth_context,
    source_readiness as read_strategy_source_readiness,
    tushare_lhb_context as read_strategy_tushare_lhb_context,
)
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
from .post_close_refresh_service import (
    PostCloseRefreshDependencies,
    run_post_close_refresh as run_post_close_refresh_isolated,
)
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
from .full_market_daily_controls_sync import sync as sync_full_market_daily_controls_isolated
from .sector_catalog_sync import sync_all as sync_all_sector_catalogs_isolated
from .ths_sector_catalog_sync import sync as sync_ths_sector_catalog_isolated
from .eastmoney_sector_members_sync import sync as sync_eastmoney_sector_members_isolated
from .eastmoney_live_hydration import hydrate as hydrate_eastmoney_live_isolated
from .ths_sector_flows import sync_industry as sync_ths_industry_isolated, sync_concept_signals as sync_ths_concept_signals_isolated
from .outcome_recomputation import recompute as recompute_outcomes_isolated
from .ths_concept_members_sync import sync as sync_ths_concept_members_isolated
from .analyst_scorecards import readiness as analyst_scorecard_readiness
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
from .tushare_catalog_fetch_service import CatalogFetchDependencies, fetch_catalog as run_catalog_fetch
from .stock_study_tushare_service import StockStudyTushareDependencies, fetch_stock_study_input
from .stock_study_service import StockStudyDependencies, build as build_stock_study_isolated
from .intraday_signal_generation import IntradaySignalGenerationDependencies, generate_intraday_signals
from .intraday_signal_event_persistence import (
    IntradaySignalEventPersistenceDependencies,
    persist_generated_signals,
)
from .intraday_scan_preparation import (
    IntradayScanPreparationDependencies,
    prepare_intraday_scan_inputs,
)
from .intraday_scan_source_status import build_scan_source_status
from .intraday_scan_signal_persistence import (
    IntradayScanPersistenceServiceDependencies,
    IntradayScanSignalPersistenceDependencies,
    persist_scan_transaction,
)
from .intraday_watch_quote_capture import WatchQuoteCaptureDependencies, capture_watch_quotes
from .intraday_watchlist_scan_service import (
    IntradayWatchlistScanDependencies,
    run_watchlist_scan,
)
from .all_board_member_backfill_service import (
    AllBoardMemberBackfillDependencies,
    run as run_all_board_member_backfill_isolated,
)
from .ths_concept_member_backfill_service import (
    ThsConceptMemberBackfillDependencies,
    run as run_ths_concept_member_backfill_isolated,
)
from .concept_limit_candidate_service import (
    ConceptLimitCandidateDependencies,
    run as run_concept_limit_candidates_isolated,
)
from .concept_limit_candidate_repository import (
    persist_candidates as persist_concept_limit_candidates,
    persist_members as persist_concept_limit_members,
    select_concepts as select_concept_limit_concepts,
)
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


def local_research_storage_governance(database: Database = db) -> dict[str, Any]:
    """Compatibility entrypoint for the isolated local storage projection."""
    return research_storage_governance_isolated(database)


_research_storage_admission = ResearchStorageAdmission(
    status_fn=local_research_storage_governance, run_database=run_database_blocking,
)


async def nonessential_high_frequency_capture_allowed() -> tuple[bool, dict[str, Any]]:
    """Use a cached, local-only budget decision to protect finite research storage.

    This deliberately gates only optional raw evidence (depth, one-second
    cross-checks and board curves).  Watchlist price evaluation, risk alerts,
    outcomes and durable delivery keep running even at the stop watermark.
    """
    return await _research_storage_admission.optional_high_frequency_allowed()


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
        values = await read_async_core_symbols(async_db)
    if not values:
        values = await read_async_analyst_claim_symbols(async_db)
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
    """Compatibility entry point for public daily evidence promotion."""
    return _persist_free_daily(
        db, provider, rows, daily_bar_type=DailyBar, parse_trade_date=tushare_date,
        decimal_or_none=decimal_or_none, upsert_bar=upsert_bar,
        persist_raw_observations=lambda _database, raw_provider, capability, raw_rows:
            persist_public_observations(raw_provider, capability, raw_rows),
    )


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


def recompute_scorecards_legacy(as_of_date: date | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias; use the isolated scorecard service."""
    return recompute_scorecards(as_of_date)
def recompute_scorecards(as_of_date: date | None = None) -> dict[str, Any]:
    """Compatibility entry point backed by local-only analyst scorecards."""
    return recompute_scorecards_isolated(as_of_date, cn_today=cn_today, db=db, readiness=analyst_scorecard_readiness)


FEATURE_VERSION = "multi-source-feature-v3"
MODEL_VERSION = "multi-source-direction-v1"
ANALYST_TEXT_FACTOR_VERSION = DEFAULT_FACTOR_VERSION


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
    return read_market_context_from_board_report(
        row, observed_at, symbol, strategy_market_state=strategy_market_state, number=intraday_number,
    )


def intraday_point_in_time_market_context(connection: Any, observed_at: datetime,
                                          symbol: str | None = None) -> dict[str, Any]:
    """Describe only the latest board snapshot known when a signal fired."""
    return read_point_in_time_market_context(
        connection, observed_at, symbol, context_from_board_report=intraday_market_context_from_board_report,
    )


def intraday_point_in_time_market_context_batch(
    connection: Any, observations: list[tuple[datetime, str]],
) -> dict[tuple[datetime, str], dict[str, Any]]:
    """Resolve point-in-time board context with one bounded report query.

    The outcome API may contain several horizons per signal.  Fetching a board
    report per row creates an N+1 read pattern; the report immediately before
    the earliest signal plus all reports through the latest signal is enough to
    reproduce the same "latest report at or before signal time" rule.
    """
    return read_point_in_time_market_context_batch(
        connection, observations, context_from_board_report=intraday_market_context_from_board_report,
    )


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
    return await read_async_open_provider_keys(async_db, capability, keys)


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
    return offline_minute_import_service.data_root()


def offline_import_path(file_name: str) -> Path:
    return offline_minute_import_service.import_path(file_name, root=offline_data_root())


def sha256_file(path: Path) -> str:
    return offline_minute_import_service.sha256_file(path)


def offline_minute_timestamp(value: Any) -> datetime:
    """Parse vendor local timestamps; naive input is Shanghai exchange time."""
    return offline_minute_import_service.minute_timestamp(value)


def offline_minute_source_available_at(row: dict[str, Any]) -> datetime | None:
    """Return a vendor-recorded availability clock without manufacturing one.

    ``bar_time`` says when a bar closed, not when a caller could have seen it.
    CSV producers may provide an explicit source/provider availability or
    receive timestamp.  Missing or blank values intentionally remain NULL so
    the file cannot be admitted to causal strategy replay by accident.
    """
    return offline_minute_import_service.source_available_at(row)


def offline_minute_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate one CSV row before it reaches the database."""
    return offline_minute_import_service.minute_row(row, decimal_or_none=decimal_or_none)


def ensure_offline_instrument(connection: Any, symbol: str) -> None:
    offline_minute_import_service.ensure_instrument(connection, symbol, exchange_for=exchange_for)


def offline_minute_import_stale_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Bound the recovery wait before a crashed local CSV import can resume."""
    return offline_minute_import_service.stale_seconds(environ)


def offline_import_recovery_action(existing: Mapping[str, Any] | None, *, now: datetime,
                                   stale_seconds: int) -> str:
    """Classify an idempotent local-file import without trusting client state."""
    return offline_minute_import_service.recovery_action(existing, now=now, stale_after_seconds=stale_seconds)


def import_offline_minute_csv(request: OfflineMinuteImportRequest) -> dict[str, Any]:
    """Stream a locally mounted minute CSV into PostgreSQL in bounded batches."""
    return offline_minute_import_service.import_csv(
        db, request, root=offline_data_root(), exchange_for=exchange_for,
        decimal_or_none=decimal_or_none, safe_error=safe_error_detail,
        stale_after_seconds=offline_minute_import_stale_seconds(),
    )


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


def full_market_daily_row_count(trade_date: date) -> int:
    """Current-date daily universe size; controls never bootstrap a date alone."""
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT count(*) AS count FROM quant.canonical_bars_daily WHERE trading_date=%s", (trade_date,),
        ).fetchone()
    return int(row["count"] if row else 0)


def full_market_daily_control_status() -> dict[str, Any]:
    """Expose latest daily control coverage without requesting a provider."""
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT trading_date,count(*)::int AS daily_rows,
                      count(*) FILTER (WHERE adj_factor IS NOT NULL)::int AS adjustment_rows,
                      count(*) FILTER (WHERE limit_up IS NOT NULL AND limit_down IS NOT NULL)::int AS limit_rows
                 FROM quant.canonical_bars_daily
                WHERE trading_date=(SELECT max(trading_date) FROM quant.canonical_bars_daily)
                GROUP BY trading_date"""
        ).fetchone()
    if not row:
        return {"state": "absent", "reason": "no canonical daily bars"}
    daily_rows = int(row["daily_rows"])
    adjustment_rows = int(row["adjustment_rows"])
    limit_rows = int(row["limit_rows"])
    ready = daily_rows > 0 and adjustment_rows == daily_rows and limit_rows == daily_rows
    return {
        "state": "ready" if ready else "blocked", "trade_date": str(row["trading_date"]),
        "daily_rows": daily_rows, "adjustment_rows": adjustment_rows, "limit_rows": limit_rows,
        "reason": None if ready else "latest canonical daily bars are missing same-date adjustment or limit controls",
    }


async def sync_full_market_daily_controls(trade_date: date) -> dict[str, Any]:
    """Fill same-date adjustment, limit and suspension controls after daily sync."""
    return await sync_full_market_daily_controls_isolated(
        trade_date,
        expected_daily_rows=full_market_daily_row_count,
        call_tushare_api=call_tushare_api,
        parse_date=tushare_date,
        persist_tushare_rows=persist_tushare_rows,
        persist_blocked=persist_tushare_fetch_blocked,
        run_database_blocking=run_database_blocking,
        db=db,
        safe_error_detail=safe_error_detail,
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
    return merge_intraday_watch_quote_prices_pure(quotes, depth_rows, number=intraday_number)


def merge_intraday_sina_watch_quotes(quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Use Sina only as a price fallback; do not invent Tencent flow fields."""
    return merge_intraday_sina_watch_quotes_pure(quotes, rows, number=intraday_number)


def merge_intraday_eastmoney_watch_flows(quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Overlay bounded Eastmoney watch-basket flow without changing price source.

    The direct Tencent depth batch remains the only decision-eligible price
    confirmation.  Eastmoney here only fills same-scan flow/turnover features
    after the all-A Tencent percentile snapshot is unavailable.
    """
    return merge_intraday_eastmoney_watch_flows_pure(quotes, rows, number=intraday_number)


def intraday_quote_observation_source(quote: dict[str, Any] | None) -> str:
    """Return the actual provider used for one persisted watch-price frame.

    The watch scan may use a same-request Tencent depth quote, an all-A
    Tencent snapshot, or a Sina fallback.  They must never be stored under the
    same provider label: a later return calculation or freshness review needs
    to know exactly which source produced the price.
    """
    return intraday_quote_observation_source_pure(quote)


def intraday_quote_exchange_time_status(quote: dict[str, Any] | None, observed_at: datetime,
                                        max_age_seconds: float) -> dict[str, Any]:
    """Classify an upstream quote timestamp against one Shanghai-clock SLO.

    Tencent emits one compact ``YYYYmmddHHMMSS`` field in its watch-depth
    adapter; Sina emits date/time separately.  Parsing is deliberately strict:
    a missing or malformed source timestamp cannot masquerade as a freshly
    fetched quote for an alert confirmation.
    """
    return intraday_quote_exchange_time_status_pure(quote, observed_at, max_age_seconds)


def intraday_quote_from_tencent(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize the limited Tencent fields used for a time-sensitive alert.

    This keeps the raw public row alongside normalized values.  ``zljlr`` is
    provider-labelled indicative main-flow data, not exchange order flow.
    """
    return intraday_quote_from_tencent_pure(
        row, symbol_from_code=eastmoney_member_symbol, number=intraday_number,
    )


def annotate_intraday_flow_percentiles(quotes: dict[str, dict[str, Any]]) -> None:
    """Attach a cross-sectional main-flow percentile without assuming units.

    Tencent's public flow unit is provider-specific, so extreme buy/sell is
    judged against the same all-A snapshot instead of a fragile absolute yuan
    threshold.  This is the cross-sectional normalization pattern used by
    factor research systems, applied only to the observed universe.
    """
    annotate_intraday_flow_percentiles_pure(quotes)


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
    return merge_persisted_limit_pool_sources(
        ths_rows, eastmoney_rows, json_safe=strategy_json_safe, number=intraday_number,
    )


def strategy_pattern_sample_candidates(as_of_date: date, max_symbols: int, per_cohort: int,
                                       focus_symbols: list[str] | None = None) -> dict[str, Any]:
    """Read persisted sample inputs, then delegate deterministic ranking."""
    inputs = load_strategy_pattern_sample_inputs(db, as_of_date)
    return pure_post_close_pattern_candidates(
        as_of_date, max_symbols, per_cohort, inputs.limit_rows,
        inputs.step_rows, inputs.prior_limit_rows,
        inputs.daily_rows, post_close_exact_board_context(as_of_date),
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
    """Compatibility entry point for bounded pattern-run persistence."""
    return persist_strategy_pattern_run_isolated(
        db, run_key, as_of_date, status, source_status, summary, samples,
        model_version=STRATEGY_PATTERN_MODEL_VERSION, json_safe=strategy_json_safe,
    )


def _strategy_pattern_mining_dependencies() -> StrategyPatternMiningDependencies:
    """Compose bounded post-close minute research without owning a provider client."""
    return StrategyPatternMiningDependencies(
        latest_date=latest_strategy_pattern_date, refresh_sources=refresh_strategy_pattern_sources,
        sample_candidates=strategy_pattern_sample_candidates,
        open_provider_capabilities=open_provider_capabilities,
        minute_capability=TENCENT_INTRADAY_MINUTE_CAPABILITY, fetch_minutes=tencent_intraday_minutes,
        intraday_pattern=intraday_limit_lift_pattern, review_score=strategy_pattern_review_score,
        persist_minute_health=persist_tencent_intraday_minute_health, persist_run=persist_strategy_pattern_run,
        run_database=run_database_blocking, model_version=STRATEGY_PATTERN_MODEL_VERSION,
        handled_errors=(asyncio.TimeoutError, httpx.HTTPError, FreeProviderError, ValueError),
    )


async def run_strategy_pattern_mining(request: StrategyPatternMiningRequest) -> dict[str, Any]:
    """Compatibility entry point for bounded, research-only pattern mining."""
    return await run_strategy_pattern_mining_isolated(request, _strategy_pattern_mining_dependencies())


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
    """Compatibility wrapper for the durable Feishu outbox service."""
    return await intraday_alert_delivery_service.attempt_delivery(
        db, delivery_id, signal_event_id, text,
        post_text=post_feishu_alert_text, run_database=run_database_blocking,
        json_safe=strategy_json_safe, recovery_text=delivery_health_recovery_text,
        max_attempts=INTRADAY_ALERT_MAX_ATTEMPTS,
    )


async def deliver_intraday_alert(signal_event_id: uuid.UUID, text: str) -> dict[str, Any]:
    """Persist before outbound I/O so a short-lived signal cannot be lost."""
    def create_pending_delivery() -> uuid.UUID:
        return intraday_alert_delivery_service.create_pending_delivery(db, signal_event_id, text)

    delivery_id = await run_database_blocking(create_pending_delivery)
    return await attempt_intraday_alert_delivery(delivery_id, signal_event_id, text)


async def retry_pending_intraday_alerts(limit: int = 3) -> dict[str, int]:
    """Retry bounded, unsent outbox rows even when their source signal faded."""
    def load_due() -> list[dict[str, Any]]:
        return intraday_alert_delivery_service.load_due_deliveries(db, INTRADAY_ALERT_MAX_ATTEMPTS, limit)

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
    return order_book_service.enabled()


def intraday_order_book_interval_seconds() -> float:
    return order_book_service.interval_seconds()


def intraday_order_book_retention_days() -> int:
    """Keep high-frequency depth evidence bounded independently of rt_k."""
    return order_book_service.retention_days()


def intraday_order_book_max_symbols() -> int:
    """Bound a single Tencent depth batch without silently losing watches."""
    return order_book_service.max_symbols()


def persist_intraday_order_book_observations(observed_at: datetime, rows: list[dict[str, Any]], latency_ms: int) -> int:
    """Persist raw order-book evidence plus derived observational features."""
    return order_book_service.persist_observations(
        db, observed_at, rows, latency_ms,
        json_safe=strategy_json_safe, record_success=record_provider_success,
    )


def persist_intraday_order_book_failure(error: str, latency_ms: int | None = None) -> None:
    order_book_service.persist_failure(
        db, error, latency_ms, record_failure=record_provider_failure,
    )


async def capture_intraday_order_book_snapshot(symbols: list[str]) -> dict[str, Any]:
    """Capture one pooled, bounded depth snapshot for the explicit watchlist."""
    return await order_book_service.capture_snapshot(
        symbols, max_symbols_value=intraday_order_book_max_symbols(),
        fetch_quotes=tencent_order_book_quotes,
        persist=persist_intraday_order_book_observations,
        persist_error=persist_intraday_order_book_failure,
        run_database=run_database_blocking,
        safe_error=safe_error_detail,
        handled_errors=(httpx.HTTPError, FreeProviderError, ValueError, ExecutorSaturatedError, asyncio.TimeoutError),
    )


async def intraday_order_book_loop() -> None:
    """Observe watchlist depth at a bounded cadence; never derive an order."""
    await run_intraday_order_book_runtime_loop(IntradayOrderBookRuntimeDependencies(
        database=db, run_database=run_database_blocking, max_symbols=intraday_order_book_max_symbols,
        realtime_session=realtime_market_session_async, open_capabilities=open_provider_capabilities,
        storage_allowed=nonessential_high_frequency_capture_allowed, capture=capture_intraday_order_book_snapshot,
        interval_seconds=intraday_order_book_interval_seconds, retention_days=intraday_order_book_retention_days,
        run_loop=intraday_order_book_runner.run_loop,
    ))


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
    """Compatibility entry point backed by the bounded minute-context service."""
    return await capture_intraday_surge_context(
        watches, mapped_peers=mapped_peers, cache=_intraday_tencent_minute_cache,
        max_symbols=intraday_minute_profile_max_symbols,
        open_capabilities=open_provider_capabilities,
        capability=TENCENT_INTRADAY_MINUTE_CAPABILITY,
        fetch_minutes=tencent_intraday_minutes,
        minute_features=intraday_minute_features,
        persist_health=persist_tencent_intraday_minute_health,
        run_database=run_database_blocking,
        safe_error=safe_error_detail,
        handled_errors=(asyncio.TimeoutError, httpx.HTTPError, FreeProviderError, ValueError),
    )


async def intraday_board_cache_evidence(observed_at: datetime) -> dict[str, Any]:
    """Expose the latest persisted board-flow age without refetching it per quote scan."""
    row = await read_async_latest_board_report(async_db)
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
    rows = await read_async_latest_fast_quotes(async_db, symbols)
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
    return persist_scan_transaction(
        IntradayScanPersistenceServiceDependencies(
            database=db,
            confirmation_window=INTRADAY_CONFIRMATION_WINDOW,
            signal_model_version=INTRADAY_SIGNAL_MODEL_VERSION,
            factor_contract_version=INTRADAY_FACTOR_CONTRACT_VERSION,
            signal_dependencies=IntradayScanSignalPersistenceDependencies(
                prepare_inputs=prepare_intraday_scan_inputs,
                preparation_dependencies=IntradayScanPreparationDependencies(
                    roll_positions_sellable=roll_paper_positions_sellable,
                    record_provider_success=record_provider_success, record_provider_failure=record_provider_failure,
                    json_safe=strategy_json_safe, persist_portfolio_snapshot=persist_portfolio_snapshot,
                    load_local_state=load_intraday_scan_local_state, clear_stale_episodes=clear_stale_signal_episodes,
                    market_context_batch=intraday_point_in_time_market_context_batch,
                    shadow_priors=latest_shadow_priors_v2, rebound_priors=latest_rebound_priors,
                    probability_profiles=load_intraday_probability_profiles,
                    daily_factors=pure_watchlist_daily_factors_by_symbol,
                    minute_volume_profiles=pure_intraday_volume_time_profiles,
                    quote_source=intraday_quote_observation_source, previous_quote_frames=previous_quote_frames,
                    first_eac_events=first_eac_breakout_events, minute_bucket=intraday_minute_bucket, number=intraday_number,
                ),
                quote_source=intraday_quote_observation_source, json_safe=strategy_json_safe,
                persist_rule_input_snapshot=persist_rule_input_snapshot,
                attach_volume_time_profile=pure_attach_volume_time_profile, number=intraday_number,
                aggregate_order_book_observations=aggregate_order_book_observations,
                generate_signals=generate_intraday_signals,
                signal_generation_dependencies=IntradaySignalGenerationDependencies(
                    base_rules=intraday_signal_rules, shadow_signal=main_wave_v2_shadow_signal,
                    rebound_signal=countertrend_rebound_realtime_signal,
                    rebound_failure_signal=countertrend_rebound_failure_reduce_signal,
                    eac_acceptance=intraday_eac_acceptance_assessment,
                ),
                load_event_state=load_intraday_signal_event_state,
                persist_generated_signals=persist_generated_signals,
                signal_event_persistence_dependencies=IntradaySignalEventPersistenceDependencies(
                    paper_risk_gate=paper_risk_gate, live_policy_gate=live_policy_gate,
                    classify_setup_state=classify_intraday_setup_state,
                    factor_contracts=intraday_factor_contracts_for_signal,
                    probability=intraday_probability_for_signal, decision_context=intraday_decision_context,
                    signal_contract=intraday_signal_contract, event_state=intraday_signal_event_state,
                    ensure_episode=ensure_signal_episode, attribution=intraday_signal_attribution,
                    paper_decision_payload=paper_decision_payload, persist_paper_decision=persist_paper_decision,
                ),
            ),
        ),
        scan_id=scan_id, observed_at=observed_at, selected_symbols=selected_symbols,
        source_status=source_status, watches=watches, quotes=quotes, tencent_rows=tencent_rows,
        quote_latency_ms=quote_latency_ms, tushare_minutes=tushare_minutes, surge_features=surge_features,
        peer_contexts=peer_contexts, fast_confirmations=fast_confirmations,
    )


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

    async def load_watches(requested_symbols: list[str]) -> list[dict[str, Any]]:
        # Fetch one extra row only to detect overflow.  It is unsafe to quietly
        # scan the first 40 while presenting the result as a full decision.
        capacity = int(intraday_watchlist_capacity(0)["max_symbols"])
        return await read_async_intraday_scan_watchlists(async_db, requested_symbols, max_symbols=capacity)

    async def persist_terminal(
        scan_id: uuid.UUID, observed_at: datetime, status: str, requested_symbols: list[str],
        source_status: dict[str, Any], summary: dict[str, Any],
    ) -> None:
        await run_database_blocking(
            persist_intraday_scan_terminal, db, scan_id, observed_at, status, requested_symbols,
            source_status, summary,
        )

    async def memberships(symbols: list[str], observed_at: datetime) -> list[dict[str, Any]]:
        return await read_async_exact_watchlist_memberships(async_db, symbols, observed_at)

    async def capture_quotes(symbols: list[str], observed_at: datetime, quote_timestamp_slo_seconds: float) -> Any:
        return await capture_watch_quotes(
            symbols, observed_at, quote_timestamp_slo_seconds,
            WatchQuoteCaptureDependencies(
                now=asyncio.get_running_loop().time, all_a_snapshot=intraday_all_a_snapshot,
                tencent_watch_quotes=tencent_order_book_quotes, sina_quotes=sina_quotes,
                eastmoney_watch_flows=eastmoney_watch_flow_quotes, quote_from_tencent=intraday_quote_from_tencent,
                merge_eastmoney_flows=merge_intraday_eastmoney_watch_flows,
                annotate_percentiles=annotate_intraday_flow_percentiles,
                annotate_flow_provenance=pure_annotate_flow_snapshot_provenance,
                merge_watch_prices=merge_intraday_watch_quote_prices, merge_sina_prices=merge_intraday_sina_watch_quotes,
                quote_freshness=intraday_quote_exchange_time_status,
                consume_background_exception=consume_background_task_exception, safe_error=safe_error_detail,
                executor_saturated_error=ExecutorSaturatedError,
                watch_quote_errors=(httpx.HTTPError, FreeProviderError, ValueError),
                all_a_snapshot_errors=(AkShareProviderError, ValueError),
            ),
        )

    async def persist_signals(*args: Any) -> list[dict[str, Any]]:
        return await run_database_blocking(persist_intraday_scan_signals, *args, timeout_seconds=60)

    payload = await run_watchlist_scan(
        request,
        IntradayWatchlistScanDependencies(
            now_utc=lambda: datetime.now(timezone.utc), new_scan_id=uuid.uuid4,
            realtime_session=realtime_market_session_async, load_watches=load_watches,
            watchlist_capacity=intraday_watchlist_capacity, persist_terminal=persist_terminal,
            prune_rule_inputs=prune_intraday_rule_input_evidence_if_due, retry_pending_alerts=retry_pending_intraday_alerts,
            load_exact_memberships=memberships, mapped_peers=pure_mapped_watchlist_peers,
            high_frequency_window=intraday_high_frequency_window, capture_quotes=capture_quotes,
            surge_context=intraday_tencent_surge_context, peer_context=intraday_peer_context,
            watch_priority_key=intraday_watch_priority_key, realtime_validation_slice=intraday_realtime_validation_slice,
            tushare_minutes=intraday_tushare_minutes, fast_confirmations=latest_intraday_fast_quote_confirmations,
            board_cache_evidence=intraday_board_cache_evidence, build_source_status=build_scan_source_status,
            persist_signals=persist_signals, deliver_alert=deliver_intraday_alert,
            alert_text=intraday_alert_text, decision_card_url=decision_card_url,
        ),
    )
    return finish(payload)


def intraday_board_curve_session(now: datetime | None = None) -> tuple[bool, str]:
    """Apply both the exchange clock and the persisted SSE holiday calendar."""
    observed_at = now or datetime.now(timezone.utc)
    active, reason = intraday_board_curve_clock_session(observed_at)
    if not active:
        return active, reason
    exchange_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    calendar_open, calendar_reason = read_sse_calendar_status(db, exchange_date)
    if not calendar_open:
        return False, calendar_reason
    return True, reason


async def intraday_board_curve_session_async(now: datetime | None = None) -> tuple[bool, str]:
    """Async-loop variant that keeps the calendar lookup off the event loop."""
    observed_at = now or datetime.now(timezone.utc)
    active, reason = intraday_board_curve_clock_session(observed_at)
    if not active:
        return active, reason
    exchange_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    calendar_open, calendar_reason = await read_async_sse_calendar_status(async_db, exchange_date)
    if not calendar_open:
        return False, calendar_reason
    return True, reason


async def open_provider_capabilities(provider_key: str, capabilities: list[str]) -> set[str]:
    """Read active circuit-breaker entries without issuing an upstream request."""
    return await read_async_open_provider_capabilities(async_db, provider_key, capabilities)


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
    await run_intraday_board_curve_runtime_loop(IntradayBoardCurveRuntimeDependencies(
        database=db, run_database=run_database_blocking, board_session=intraday_board_curve_session_async,
        storage_allowed=nonessential_high_frequency_capture_allowed, capture=capture_intraday_board_flow_curve,
        curve_retention_days=intraday_board_curve_retention_days,
        rotation_retention_days=intraday_board_rotation_retention_days,
        run_loop=intraday_board_curve_runner.run_loop,
    ))


def strategy_review_automation_enabled() -> bool:
    return os.getenv("STRATEGY_REVIEW_AUTOMATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def post_close_strategy_automation_enabled() -> bool:
    return os.getenv("POST_CLOSE_STRATEGY_AUTOMATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def daily_summary_automation_enabled() -> bool:
    return os.getenv("DAILY_SUMMARY_AUTOMATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def sse_calendar_open(calendar_date: date) -> bool:
    """Compatibility entry point for the isolated persisted SSE gate."""
    return read_sse_calendar_open(db, calendar_date)


async def sse_calendar_open_async(calendar_date: date) -> bool:
    """Compatibility entry point for the isolated async persisted SSE gate."""
    return await read_async_sse_calendar_open(async_db, calendar_date)


async def strategy_review_loop() -> None:
    """Run the isolated checkpoint scheduler through the production adapter."""
    await run_strategy_review_runtime_loop(StrategyReviewRuntimeDependencies(
        database=db, run_database=run_database_blocking, calendar_open=sse_calendar_open_async,
        sync_index_context=sync_strategy_index_context, build_market_snapshot=build_market_snapshot,
        market_snapshot_request=lambda session: MarketSnapshotRequest(session=session, refresh_public_quotes=True),
        build_board_report=run_intraday_board_report, recompute_outcomes=recompute_outcomes,
        recompute_analyst_intraday_outcomes=recompute_analyst_intraday_outcomes_for_date,
        recompute_scorecards=recompute_scorecards, strategy_review_payload=strategy_review_payload,
        strategy_review_request=StrategyReviewRequest, completed_for_checkpoint=review_checkpoint_completed_isolated,
        build_analyst_market_review=build_recorded_analyst_market_review,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
        scheduler=strategy_review_scheduler,
    ))


async def post_close_strategy_loop() -> None:
    """Run the same-date post-close scheduler through the production adapter."""
    await run_post_close_strategy_runtime_loop(PostCloseStrategyRuntimeDependencies(
        database=db, run_database=run_database_blocking, calendar_open=sse_calendar_open_async,
        retry_window=post_close_strategy_retry_window,
        strategy_completed_for_date=post_close_strategy_completed_for_date,
        main_wave_completed_for_date=watchlist_main_wave_completed_for_date,
        run_recorded=run_recorded, run_post_close_strategy=run_post_close_strategy,
        post_close_request=PostCloseStrategyRequest, post_close_model_version=POST_CLOSE_STRATEGY_MODEL_VERSION,
        run_main_wave_research=persist_watchlist_main_wave_research,
        main_wave_request=WatchlistMainWaveResearchRequest,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
        scheduler=post_close_strategy_scheduler,
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
    """Persist the frontend-only daily summary through its runtime adapter."""
    return await run_daily_strategy_summary_runtime(exchange_date, _daily_strategy_summary_runtime_dependencies())

async def daily_strategy_summary_loop() -> None:
    """Run the frontend-only daily summary scheduler through its adapter."""
    await run_daily_strategy_summary_runtime_loop(_daily_strategy_summary_runtime_dependencies())


def _daily_strategy_summary_runtime_dependencies() -> DailyStrategySummaryRuntimeDependencies:
    return DailyStrategySummaryRuntimeDependencies(
        database=db, run_database=run_database_blocking, build_summary=build_daily_strategy_summary,
        summary_text=daily_strategy_summary_text,
        dashboard_url=lambda: (os.getenv("QUANT_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/") or None,
        json_safe=strategy_json_safe, json_value=Json, terminal_for_exchange_date=daily_summary_terminal_isolated,
        calendar_open=sse_calendar_open_async,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
        scheduler=daily_strategy_summary_scheduler,
    )


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
    return await intraday_fast_quote_capture_service.capture(
        symbol, call_provider=call_tushare_api, run_database=run_database_blocking,
        persist_quote=persist_intraday_super_get_fast_quote,
        persist_failure=record_intraday_super_get_fast_quote_failure,
        number=intraday_number, safe_error=safe_error_detail,
        is_circuit_open=lambda error: isinstance(error, HTTPException) and is_circuit_open_http_error(error),
    )


async def intraday_super_get_fast_quote_loop() -> None:
    """Run the optional one-second rt_k cross-check in special windows."""
    await run_intraday_fast_quote_runtime_loop(IntradayFastQuoteRuntimeDependencies(
        database=db, run_database=run_database_blocking, max_symbols=intraday_super_get_fast_max_symbols,
        watch_priority_key=intraday_watch_priority_key, realtime_session=realtime_market_session_async,
        high_frequency_window=intraday_high_frequency_window,
        storage_allowed=nonessential_high_frequency_capture_allowed,
        capture_quote=capture_intraday_super_get_fast_quote, observe_completed=observe_completed_task,
        interval_seconds=intraday_super_get_fast_interval_seconds,
        max_in_flight=intraday_super_get_fast_max_in_flight,
        retention_days=intraday_fast_quote_retention_days, run_loop=run_intraday_fast_quote_loop,
    ))


async def intraday_minute_profile_capture_loop() -> None:
    """Capture the explicit-watch EAC baseline once near each A-share close.

    Tencent minute tapes are requested during the final continuous-auction
    window. A failed fetch may retry during the short 14:55--14:59 window; a
    completed or partial capture is never repeated that day.
    """
    await run_intraday_minute_profile_runtime_loop(IntradayMinuteProfileRuntimeDependencies(
        database=db, run_database=run_database_blocking,
        max_symbols=intraday_minute_profile_max_symbols, watch_priority_key=intraday_watch_priority_key,
        calendar_open=sse_calendar_open_async, storage_allowed=nonessential_high_frequency_capture_allowed,
        capture=capture_intraday_minute_sessions, run_loop=intraday_minute_profile_runner.run_loop,
    ))


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
    async def fetch_report() -> dict[str, Any]:
        # Persist the full bounded Top10 requested by the close-review surface.
        # ``quoted_members`` remains visible so sparse public quote coverage is not
        # presented as complete membership coverage.
        return await intraday_sector_report(IntradaySectorReportRequest(kind="all", top_stocks=10, hydrate_top_boards=0))

    return await intraday_board_report_service.run(
        database=db, fetch_report=fetch_report, board_candidates=board_stock_mining_candidates,
        persist_mining_run=persist_board_stock_mining_run, refresh_limit_anchors=refresh_intraday_limit_up_anchors,
        run_limit_linkage=run_limit_linkage_mining, run_database=run_database_blocking,
        json_safe=strategy_json_safe, flow_label=intraday_flow_label, number=intraday_number,
        safe_error=safe_error_detail,
    )


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
    """Compatibility facade for stored-only review context."""
    return read_strategy_index_breadth_context(
        connection, as_of_date, session, observed_at,
        index_symbols=STRATEGY_INDEX_SYMBOLS,
        index_regime=strategy_index_regime,
        number=number,
    )


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


async def intraday_decision_card_async(symbol: str) -> dict[str, Any]:
    """Native-async local decision-card path for dashboard refreshes."""
    return await read_async_intraday_decision_card(
        async_db, symbol,
        strategy_market_state_fn=strategy_market_state,
        classify_text=classify_remote_text,
        factor_version=ANALYST_TEXT_FACTOR_VERSION,
        promotion_key=PROMOTION_KEY,
        max_approved_weight=MAX_APPROVED_WEIGHT,
        json_safe_fn=strategy_json_safe,
    )


def strategy_intraday_candidates(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Turn an exact board-member join into transparent, bounded candidates.

    Board scores use within-taxonomy ranks.  Stock scores then use Tencent's
    relative main flow, volume ratio, turnover and price change.  The function
    is deliberately pure so its time semantics and risk gates are testable.
    """
    return select_intraday_candidates(items, limit, rank=strategy_rank, number=intraday_number)


def strategy_event_context(symbols: list[str], observed_at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read only evidence that was available by the snapshot time.

    龙虎榜/涨停池 events are returned as next-session context, never as a
    same-day intraday score component.
    """
    return read_strategy_event_context(db, symbols, observed_at)


def strategy_tushare_lhb_context(symbols: list[str], observed_at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read Tushare龙虎榜 evidence already available at the snapshot time.

    `top_list`/`top_inst` are post-close facts.  They deliberately remain
    explanation-only and cannot influence a same-day intraday rank.
    """
    return read_strategy_tushare_lhb_context(db, symbols, observed_at)


def strategy_source_readiness(observed_at: datetime) -> dict[str, Any]:
    """Expose source freshness and ownership without inventing source parity."""
    return read_strategy_source_readiness(
        db, observed_at, provider_status=free_provider_status, json_safe=strategy_json_safe,
    )


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
    """Compatibility entry point for one exact member-coverage batch."""
    return await run_all_board_member_backfill_isolated(
        request,
        AllBoardMemberBackfillDependencies(
            sync_all_ths_catalogs=sync_all_ths_sector_catalogs,
            sync_ths_catalog=sync_ths_sector_catalog,
            ths_request=SectorCatalogSyncRequest,
            sync_eastmoney_members=sync_eastmoney_board_members,
            eastmoney_request=EastmoneyBoardMemberSyncRequest,
            http_exception=HTTPException,
        ),
    )


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
    """Compatibility entry point for a fail-closed exact member batch."""
    async def existing(trade_date: date) -> Any:
        return await read_async_ths_concept_flow_rows(async_db, trade_date)

    async def progress(trade_date: date) -> Any:
        return await read_async_ths_concept_member_progress(async_db, trade_date)

    return await run_ths_concept_member_backfill_isolated(
        request,
        ThsConceptMemberBackfillDependencies(
            china_today=lambda: datetime.now(ZoneInfo("Asia/Shanghai")).date(),
            load_existing_flow=existing, sync_flow_catalog=sync_ths_concept_signals,
            flow_request=SectorFlowSyncRequest, sync_members=sync_ths_concept_members,
            member_request=ConceptMemberSyncRequest, load_progress=progress,
        ),
    )


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
    """Build exact concept/limit-up candidates through the isolated service."""
    async def select_concepts(trade_date: date | None, top_concepts: int) -> tuple[date | None, list[Any]]:
        return await run_database_blocking(select_concept_limit_concepts, db, trade_date, top_concepts)

    async def load_rows(request_key: str) -> list[dict[str, Any]]:
        return await run_database_blocking(tushare_rows_for_request, request_key)

    async def persist_members(sector_key: str, rows: list[dict[str, Any]], provider: str, observed_at: datetime) -> int:
        return await run_database_blocking(
            persist_concept_limit_members, db, sector_key, rows, provider, observed_at,
            persist_ths_sector_members,
        )

    async def persist_candidates(
        selected_date: date, concepts: list[Any], concept_keys: list[str], limit_provider: str,
        limit_by_symbol: dict[str, dict[str, Any]], membership_status: dict[str, str], observed_at: datetime,
        leaders_per_concept: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        return await run_database_blocking(
            persist_concept_limit_candidates, db, selected_date, concepts, concept_keys, limit_provider,
            limit_by_symbol, membership_status, observed_at, leaders_per_concept,
            study_number, decimal_or_none, Json,
        )

    return await run_concept_limit_candidates_isolated(
        request,
        ConceptLimitCandidateDependencies(
            select_concepts=select_concepts, now_utc=lambda: datetime.now(timezone.utc),
            fetch_catalog=fetch_tushare_catalog, request=TushareFetchRequest, load_rows=load_rows,
            persist_members=persist_members, persist_candidates=persist_candidates,
            http_exception=HTTPException,
        ),
    )


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
    return await read_async_realtime_market_session(async_db, api_name, now)


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


async def _post_close_core_symbols(limit: int) -> list[str]:
    return await read_async_limited_core_symbols(async_db, limit)


def _post_close_refresh_dependencies() -> PostCloseRefreshDependencies:
    """Compose the local-only boundaries of the post-close application service."""
    return PostCloseRefreshDependencies(
        database=db, china_today=cn_today, provider_configs=provider_configs, run_database=run_database_blocking,
        reconcile_stale_fetch_runs=reconcile_stale_fetch_runs, reprocess_remote_reports=reprocess_remote_reports,
        sync_market_universe=sync_market_universe, sync_full_market_daily=sync_full_market_daily,
        sync_strategy_index_context=sync_strategy_index_context, build_market_snapshot=build_market_snapshot,
        load_core_symbols=_post_close_core_symbols, akshare_probe=akshare_probe,
        sync_ths_industry_flow=sync_ths_industry_moneyflow, sync_ths_concept_flow=sync_ths_concept_signals,
        rebuild_market_flow_features=rebuild_stored_market_flow_features,
        refresh_pattern_sources=refresh_strategy_pattern_sources, run_pattern_mining=run_strategy_pattern_mining,
        sync_daily_controls=sync_full_market_daily_controls, sync_cninfo_announcements=sync_cninfo_announcements,
        run_board_report=run_intraday_board_report, run_strategy_decision=run_strategy_decision,
        persist_close_review=_persist_close_review, recompute_outcomes=recompute_outcomes,
        recompute_intraday_outcomes=recompute_analyst_intraday_outcomes_for_date,
        recompute_scorecards=recompute_scorecards, rebuild_analyst_research=rebuild_analyst_research_for_date,
        run_post_close_strategy=run_post_close_strategy, persist_watchlist_main_wave=persist_watchlist_main_wave_research,
        build_research_snapshot=build_snapshot, run_orchestrator=run_post_close_refresh_orchestrated,
        record_stage=record_stage_with_receipt, lease_key=POST_CLOSE_REFRESH_LEASE_KEY,
        lease_seconds=post_close_refresh_lease_seconds, acquire_lease=acquire_runtime_lease,
        renew_lease=renew_runtime_lease, release_lease=release_runtime_lease,
        safe_error_detail=safe_error_detail, json_safe=strategy_json_safe,
    )


async def run_post_close_refresh(request: PostCloseRefreshRequest) -> dict[str, Any]:
    """Compatibility entry point for the isolated same-date refresh assembly."""
    return await run_post_close_refresh_isolated(request, _post_close_refresh_dependencies())


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


def _tushare_fetch_ledger_dependencies() -> TushareFetchLedgerDependencies:
    return TushareFetchLedgerDependencies(
        database=db, json_value=Json, looks_like_response_header=looks_like_response_header,
        normalize_cached_rows=normalize_tushare_rows, persist_rows=persist_tushare_rows,
        record_provider_failure=record_provider_failure, record_provider_success=record_provider_success,
        record_provider_capability=record_provider_api_capability,
        provider_error_availability=provider_error_availability, provider_call_error=ProviderCallError,
        safe_error_detail=safe_error_detail,
    )


def prepare_tushare_fetch_run(request: TushareFetchRequest, request_key: str, candidate_keys: list[str],
                              canonical_params: dict[str, Any]) -> dict[str, Any] | None:
    return prepare_tushare_fetch_run_isolated(
        request, request_key, candidate_keys, canonical_params, _tushare_fetch_ledger_dependencies(),
    )


def persist_tushare_fetch_success(request: TushareFetchRequest, request_key: str, bounded_rows: list[dict[str, Any]],
                                  truncated: bool, result: Any, provider_latency_ms: int | None = None) -> tuple[str, int]:
    return persist_tushare_fetch_success_isolated(
        request, request_key, bounded_rows, truncated, result, _tushare_fetch_ledger_dependencies(), provider_latency_ms,
    )


def persist_tushare_fetch_cancel(request_key: str, api_name: str, candidate_keys: list[str]) -> None:
    return persist_tushare_fetch_cancel_isolated(request_key, api_name, candidate_keys, _tushare_fetch_ledger_dependencies())


def persist_tushare_fetch_failure(request_key: str, api_name: str, candidate_keys: list[str], error: Exception,
                                  provider_latency_ms: int | None = None) -> None:
    return persist_tushare_fetch_failure_isolated(
        request_key, api_name, candidate_keys, error, _tushare_fetch_ledger_dependencies(), provider_latency_ms,
    )


def persist_tushare_fetch_blocked(request_key: str, error: Exception) -> None:
    return persist_tushare_fetch_blocked_isolated(request_key, error, _tushare_fetch_ledger_dependencies())


async def fetch_tushare_catalog(request: TushareFetchRequest) -> dict[str, Any]:
    return await run_catalog_fetch(
        request,
        CatalogFetchDependencies(
            realtime_market_hours_apis=REALTIME_MARKET_HOURS_APIS,
            realtime_market_session=realtime_market_session_async,
            provider_candidates=provider_candidates,
            circuit_open_provider_keys=circuit_open_provider_keys_async,
            run_database=run_database_blocking,
            prepare_run=prepare_tushare_fetch_run,
            persist_success=persist_tushare_fetch_success,
            persist_cancel=persist_tushare_fetch_cancel,
            persist_failure=persist_tushare_fetch_failure,
            persist_blocked=persist_tushare_fetch_blocked,
            call_api=call_tushare_api,
            looks_like_response_header=looks_like_response_header,
            realtime_rows_are_current=realtime_rows_are_current,
            catalog=TUSHARE_CATALOG,
            normalized_apis=CORE_NORMALIZED_APIS,
            provider_call_error=ProviderCallError,
            executor_saturated_error=ExecutorSaturatedError,
            local_capacity_detail=LOCAL_CAPACITY_HTTP_DETAIL,
        ),
    )


def tushare_rows_for_request(request_key: str) -> list[dict[str, Any]]:
    """Read the immutable raw evidence associated with one bounded fetch."""
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT row_data FROM quant.tushare_raw_records WHERE request_key=%s ORDER BY record_index",
            (request_key,),
        ).fetchall()
    return [dict(row["row_data"]) for row in rows]


async def stock_study_fetch(label: str, request: TushareFetchRequest) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return await fetch_stock_study_input(
        label,
        request,
        StockStudyTushareDependencies(
            fetch_catalog=fetch_tushare_catalog,
            run_database=run_database_blocking,
            raw_rows_for_request=tushare_rows_for_request,
            looks_like_response_header=looks_like_response_header,
            is_local_capacity_error=is_local_capacity_http_error,
            is_circuit_open_error=is_circuit_open_http_error,
        ),
    )


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
    """Compatibility export for the isolated stock-study readiness repository."""
    return read_raw_api_window_summary(connection, api_name, symbol, start_date, end_date)


def stock_window_readiness(symbol: str, start_date: date, end_date: date) -> dict[str, Any]:
    """Compatibility export for the isolated stock-study readiness repository."""
    return read_stock_window_readiness(db, symbol, start_date, end_date)


def stock_study_claims(symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compatibility export for the isolated stock-study evidence repository."""
    return read_stock_study_claims(db, symbol)


async def build_stock_study(symbol: str, request: StockStudyRequest) -> dict[str, Any]:
    return await build_stock_study_isolated(
        symbol, request,
        StockStudyDependencies(
            china_today=cn_today, tushare_request=TushareFetchRequest, daily_sync_request=TushareSyncRequest,
            fetch_tushare=stock_study_fetch, realtime_market_session=realtime_market_session_async,
            sync_baostock=sync_baostock, free_fetch=stock_study_free_fetch,
            eastmoney_daily=eastmoney_daily, eastmoney_quote=eastmoney_quote,
            run_akshare=run_akshare_blocking, akshare_daily=akshare_daily,
            tencent_daily=tencent_daily, sina_quote=sina_quote, cninfo_announcements=cninfo_announcements,
            run_database=run_database_blocking, persist_market_events=persist_market_events,
            persist_announcement_health=persist_announcement_provider_health, technical_summary=technical_summary,
            analyst_claims=stock_study_claims, recent_events=recent_market_events,
            window_readiness=stock_window_readiness, latest_row=latest_study_row,
        ),
    )


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


def _start_application_background_tasks() -> dict[str, asyncio.Task[None]]:
    """Create the uniquely-labelled leased runtime loops after local startup."""
    interval_seconds = intraday_scan_interval_seconds()
    lease_holder_id = uuid.uuid4()
    lease_seconds = background_loop_lease_seconds()

    async def leased_background_loop(label: str, factory: Callable[[], Awaitable[None]]) -> None:
        lease_key = f"background_loop:{label}"
        async def acquire() -> bool:
            return await acquire_background_runtime_lease(async_db, lease_key, lease_holder_id, lease_seconds)
        async def renew() -> bool:
            return await renew_background_runtime_lease(async_db, lease_key, lease_holder_id, lease_seconds)
        async def release() -> None:
            await release_background_runtime_lease(async_db, lease_key, lease_holder_id)
        await supervise_leased_loop(
            label, factory, acquire, renew, release, lease_seconds,
            on_state=background_loop_registry.mark,
        )

    return start_leased_background_tasks((
        BackgroundTaskSpec("intraday_monitor", interval_seconds >= 30, lambda: intraday_monitor_loop(interval_seconds)),
        BackgroundTaskSpec("super_get_fast_quote", interval_seconds >= 30, intraday_super_get_fast_quote_loop),
        BackgroundTaskSpec("strategy_review", strategy_review_automation_enabled(), strategy_review_loop),
        BackgroundTaskSpec("post_close_strategy", post_close_strategy_automation_enabled(), post_close_strategy_loop),
        BackgroundTaskSpec("daily_strategy_summary", daily_summary_automation_enabled(), daily_strategy_summary_loop),
        BackgroundTaskSpec("ths_member_backfill", ths_concept_member_backfill_enabled(), ths_concept_member_backfill_loop),
        BackgroundTaskSpec("all_board_member_backfill", all_board_member_backfill_enabled(), all_board_member_backfill_loop),
        BackgroundTaskSpec("minute_profile_capture", intraday_minute_profile_capture_enabled(), intraday_minute_profile_capture_loop),
        BackgroundTaskSpec("tencent_order_book", intraday_order_book_enabled() and interval_seconds >= 30, intraday_order_book_loop),
        BackgroundTaskSpec("board_flow_curve", intraday_board_curve_enabled(), intraday_board_flow_curve_loop),
    ), leased_background_loop)


def _application_lifecycle_dependencies() -> ApplicationLifecycleDependencies:
    return ApplicationLifecycleDependencies(
        open_database=db.open,
        open_async_database=async_db.open,
        configure_request_reserver=configure_provider_request_reserver,
        request_reserver=reserve_tushare_provider_request_slot,
        max_reservation_wait_seconds=provider_global_rate_limit_max_wait_seconds(),
        initialize_provider_metrics=_initialize_provider_metrics,
        start_http_clients=start_http_clients,
        legacy_schema_bootstrap_enabled=legacy_schema_bootstrap_enabled,
        migrate_database=db.migrate,
        verify_versioned_schema=db.verify_versioned_schema,
        ensure_catalog_capabilities=ensure_catalog_capabilities,
        run_database=run_database_blocking,
        start_background_tasks=_start_application_background_tasks,
        cancel_background_tasks=cancel_background_tasks,
        cancel_shared_snapshots=_intraday_all_a_snapshots.cancel_inflight,
        shutdown_super_get_executor=shutdown_super_get_executor,
        shutdown_runtime_executors=shutdown_runtime_executors,
        close_http_clients=close_http_clients,
        close_async_database=async_db.close,
        close_database=db.close,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with application_lifespan(_application_lifecycle_dependencies()):
        yield


def _initialize_provider_metrics() -> None:
    for configured_provider in provider_configs().values():
        provider_shared_rate_limit_wait_seconds.labels(configured_provider.key)
        provider_shared_rate_limit_rejections_total.labels(configured_provider.key)


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


app.include_router(build_provider_status_router(db, provider_status, free_provider_status, async_database=async_db))
app.include_router(build_research_readiness_router(
    db, historical_estimate_from_db, feature_readiness_state, historical_replay_readiness, async_db,
))
app.include_router(build_analyst_reads_router(db, remote_report_list_state, analyst_text_factor_summary, async_database=async_db))
app.include_router(build_analyst_trade_action_reads_router(db, anqiang_trade_action_replay, async_database=async_db))
app.include_router(build_analyst_action_outcomes_router(
    db, materialize_anqiang_action_replay_outcomes, async_database=async_db,
))
app.include_router(build_analyst_skill_reads_router(db, analyst_skill_profiles, async_database=async_db))
app.include_router(build_analyst_research_reads_router(db, analyst_research_status, async_database=async_db))
app.include_router(build_automation_reads_router(db, async_database=async_db))
app.include_router(build_event_reads_router(db, async_db))
app.include_router(build_strategy_reads_router(db, STRATEGY_DECISION_MODEL_VERSION, async_db))
app.include_router(build_paper_reads_router(db, async_db))
app.include_router(build_paper_actions_router(db, configure_paper_account, accept_paper_decision))
app.include_router(build_analyst_prompt_lab_router(
    db, materialize_prompt_candidates, label_prompt_candidate, evaluate_prompt_variant,
    materialize_intraday_analyst_outcomes, async_database=async_db,
))
app.include_router(build_strategy_pattern_reads_router(
    db, merge_limit_pool_sources, limit_board_count, strategy_json_safe,
    post_close_limit_daily_features, post_close_exact_board_context, post_close_tushare_lhb_context, async_db,
    run_database_blocking,
))
app.include_router(build_board_rotation_reads_router(db, async_database=async_db))
app.include_router(build_board_stock_mining_reads_router(db, async_database=async_db))
app.include_router(build_limit_linkage_mining_reads_router(db, async_database=async_db))
app.include_router(build_board_curve_reads_router(
    db, intraday_board_curve_retention_days, intraday_board_rotation_retention_days, async_database=async_db,
))
app.include_router(build_market_flow_reads_router(db, async_database=async_db))
app.include_router(build_research_catalog_reads_router(db, async_db))
app.include_router(build_intraday_outcome_reads_router(
    db, intraday_point_in_time_market_context_batch, intraday_signal_attribution, intraday_outcome_attribution_summary,
    async_database=async_db,
    market_context_from_board_report_fn=intraday_market_context_from_board_report,
))
app.include_router(build_sector_reads_router(
    db, ths_concept_member_backfill_enabled, ths_concept_member_backfill_batch_size, async_database=async_db,
))
app.include_router(build_intraday_evidence_reads_router(
    db, intraday_decision_card, async_database=async_db,
    async_decision_card_fn=intraday_decision_card_async,
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
            optional_background_tasks=lambda: {
                "background_loop:ths_member_backfill": ths_concept_member_backfill_enabled(),
                "background_loop:all_board_member_backfill": all_board_member_backfill_enabled(),
            },
            daily_control_plane_status=full_market_daily_control_status,
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


def _research_maintenance_dependencies() -> ResearchMaintenanceDependencies:
    return ResearchMaintenanceDependencies(
        database=db, china_today=cn_today, exchange_for=exchange_for,
        rebuild_analyst_research=rebuild_analyst_research,
        sync_universe_membership_history=sync_universe_membership_history, http_exception=HTTPException,
    )


def update_analyst_research_profile(analyst_id: str, payload: AnalystResearchProfileRequest) -> dict[str, Any]:
    return update_analyst_research_profile_isolated(analyst_id, payload, _research_maintenance_dependencies())


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
    return update_universe_members_isolated(payload, _research_maintenance_dependencies())


def build_features(payload: GenerateRequest) -> dict[str, Any]:
    return build_feature_snapshot(payload.as_of_date or cn_today(), payload.universe_key)


def latest_features(universe_key: str = "core", limit: int = 200) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.latest_features(db, universe_key, limit)


def _research_experiment_dependencies() -> ResearchExperimentDependencies:
    return ResearchExperimentDependencies(
        database=db, china_today=cn_today, as_utc=as_utc, http_exception=HTTPException,
        evaluate_factor_set=evaluate_factor_set, run_multi_factor_strategy=run_multi_factor_strategy_sql,
        json_value=Json,
    )


def research_window(connection: Any, universe_key: str, start_date: date | None, end_date: date | None) -> tuple[date, date]:
    """Compatibility export for the isolated local experiment window."""
    return research_window_isolated(connection, universe_key, start_date, end_date, http_exception=HTTPException)


def factor_registry() -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.factor_registry(db)


def evaluate_factors(payload: FactorEvaluationRequest) -> dict[str, Any]:
    return evaluate_factors_isolated(payload, _research_experiment_dependencies())


def factor_evaluations(universe_key: str = "core", limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.factor_evaluations(db, universe_key, limit)


def strategy_registry() -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.strategy_registry(db)


def backtest_strategy(payload: StrategyBacktestRequest) -> dict[str, Any]:
    return backtest_strategy_isolated(payload, _research_experiment_dependencies())


def strategy_experiments(universe_key: str = "core", limit: int = 50) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.strategy_experiments(db, universe_key, limit)


def reconcile_stale_fetch_runs(payload: FetchRunReconcileRequest) -> dict[str, Any]:
    return reconcile_stale_fetch_runs_isolated(payload, _research_maintenance_dependencies())


def data_quality_issues(limit: int = 100) -> dict[str, Any]:
    """Compatibility export for the research-catalog read model."""
    return research_catalog_reads.data_quality_issues(db, limit)


def build_snapshot(payload: SnapshotRequest) -> dict[str, Any]:
    return build_snapshot_isolated(payload, _research_experiment_dependencies())


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


def _intraday_watchlist_dependencies() -> IntradayWatchlistDependencies:
    return IntradayWatchlistDependencies(
        database=db, run_database=run_database_blocking, hydrate_history=hydrate_watchlist_history,
        exchange_for=exchange_for, json_value=Json, http_exception=HTTPException,
    )


async def upsert_intraday_watchlist(symbol: str, payload: IntradayWatchlistRequest) -> dict[str, Any]:
    return await upsert_intraday_watchlist_isolated(symbol, payload, _intraday_watchlist_dependencies())


async def sync_intraday_watchlist_history(symbol: str) -> dict[str, Any]:
    return await sync_intraday_watchlist_history_isolated(symbol, _intraday_watchlist_dependencies())


async def delete_intraday_watchlist(symbol: str) -> dict[str, Any]:
    return await delete_intraday_watchlist_isolated(symbol, _intraday_watchlist_dependencies())


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

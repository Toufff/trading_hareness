from __future__ import annotations
import asyncio
import functools
import hashlib
import json
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import psycopg
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
    akshare_limit_pool_events,
    akshare_macro_cross_asset_supplements,
    akshare_market_summary,
    akshare_market_breadth,
    akshare_moneyflow_supplements,
    akshare_status,
    akshare_strong_pool_events,
)
from .fuyao_provider import FuyaoProviderError, all_a_snapshot_rows as fuyao_all_a_snapshot_rows
from .limit_up_anchor import live_limit_up_pool_rows
from .analysis import as_utc
from .capability_registry import api_capability
from .database import AsyncDatabase, Database
from .settings import Settings
from . import daily_control_plane
from .daily_control_plane import (
    DailyControlPlaneSyncDependencies,
    EQUITY_DAILY_CONTROL_STATUS_SQL,
    status_payload as daily_control_plane_status_payload,
)
from .async_provider_circuit_repository import open_capabilities as read_async_open_provider_capabilities
from .async_provider_circuit_repository import open_provider_keys as read_async_open_provider_keys
from .async_market_session_repository import realtime_market_session as read_async_realtime_market_session
from .async_market_session_repository import sse_calendar_open as read_async_sse_calendar_open
from .async_market_session_repository import sse_calendar_status as read_async_sse_calendar_status
from .daily_bar_repository import exchange_for, upsert_daily_bar
from .sector_membership_repository import (
    persist_observed_snapshot as persist_observed_sector_snapshot,
    persist_ths_snapshot as persist_ths_sector_snapshot,
)
from .public_market_repository import (
    persist_free_daily as _persist_free_daily,
    persist_free_quote as _persist_free_quote,
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
)
from .research_maintenance_service import (
    ResearchMaintenanceDependencies,
    reconcile_stale_automation_runs as reconcile_stale_automation_runs_isolated,
    reconcile_stale_fetch_runs as reconcile_stale_fetch_runs_isolated,
    update_analyst_profile as update_analyst_research_profile_isolated,
    update_universe_members as update_universe_members_isolated,
)
from .intraday_watchlist_service import (
    IntradayWatchlistDependencies,
    WatchlistHistoryHydrationDependencies,
    delete as delete_intraday_watchlist_isolated,
    hydrate_watchlist_history as intraday_watchlist_service_hydrate_history,
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
from .intraday_derived_flow_metrics import (
    apply_derived_watch_flow_metrics as pure_apply_derived_watch_flow_metrics,
    derive_watch_flow_metrics as pure_derive_watch_flow_metrics,
    derived_flow_divergence as pure_derived_flow_divergence,
)
from .intraday_quote_normalization import (
    annotate_flow_percentiles as annotate_intraday_flow_percentiles_pure,
    exchange_time_status as intraday_quote_exchange_time_status_pure,
    merge_eastmoney_watch_flows as merge_intraday_eastmoney_watch_flows_pure,
    merge_longhu_watch_quotes as merge_intraday_longhu_watch_quotes_pure,
    merge_sina_watch_quotes as merge_intraday_sina_watch_quotes_pure,
    merge_watch_quote_prices as merge_intraday_watch_quote_prices_pure,
    observation_source as intraday_quote_observation_source_pure,
    quote_from_fuyao as intraday_quote_from_fuyao_pure,
)
from .intraday_decision_card_read_model import decision_card as read_intraday_decision_card
from . import intraday_signal_attribution_service
from .intraday_signal_attribution_service import IntradaySignalAttributionRefreshDependencies
from .async_intraday_decision_card_repository import decision_card as read_async_intraday_decision_card
from .async_intraday_scan_preflight_repository import latest_board_report as read_async_latest_board_report
from .async_intraday_scan_preflight_repository import latest_fast_quotes as read_async_latest_fast_quotes
from .async_intraday_scan_inputs_repository import exact_memberships as read_async_exact_watchlist_memberships
from .async_intraday_scan_inputs_repository import enabled_watches as read_async_enabled_intraday_watches
from .async_intraday_scan_inputs_repository import watchlists as read_async_intraday_scan_watchlists
from .async_intraday_scan_inputs_repository import watch_flow_reference as read_async_watch_flow_reference
from .ten_day_leader_rotation_read_repository import latest_ten_day_leader_rotation_pool as read_async_ten_day_leader_rotation_pool
from .async_ths_concept_member_backfill_repository import existing_flow_rows as read_async_ths_concept_flow_rows
from .async_ths_concept_member_backfill_repository import member_progress as read_async_ths_concept_member_progress
from .async_sync_symbol_repository import analyst_claim_symbols as read_async_analyst_claim_symbols
from .async_sync_symbol_repository import core_symbols as read_async_core_symbols
from .async_sync_symbol_repository import limited_core_symbols as read_async_limited_core_symbols
from .sync_symbol_repository import analyst_claim_symbols as read_sync_analyst_claim_symbols
from .sync_symbol_repository import core_symbols as read_sync_core_symbols
from .async_runtime_lease_repository import acquire as acquire_background_runtime_lease
from .async_runtime_lease_repository import release as release_background_runtime_lease
from .async_runtime_lease_repository import renew as renew_background_runtime_lease
from .async_intraday_alert_outbox_repository import create_pending as create_async_pending_intraday_alert_delivery
from .async_intraday_alert_outbox_repository import due_deliveries as read_async_due_intraday_alert_deliveries
from .intraday_volume_profiles import attach_volume_time_profile as pure_attach_volume_time_profile
from .intraday_volume_profiles import volume_time_profile as pure_intraday_volume_time_profile
from .intraday_volume_profiles import volume_time_profiles as pure_intraday_volume_time_profiles
from .intraday_minute_provider_service import fetch_bounded_minute_context
from .intraday_surge_context_service import capture as capture_intraday_surge_context
from .strategy_candidate_ranking import select as select_intraday_candidates
from .xiaojie_leader_flow import MODEL_VERSION as XIAOJIE_LEADER_FLOW_MODEL_VERSION, evaluate_snapshot as evaluate_xiaojie_leader_flow_snapshot
from .xiaojie_leader_flow_runtime import (
    XiaojieLeaderFlowDependencies,
    persist_xiaojie_signal_event,
    run_xiaojie_leader_flow as xiaojie_leader_flow_runtime_run,
)
from .xiaojie_reference_repository import (
    ensure_session_trade_limits as ensure_xiaojie_session_trade_limits,
    load_session_reference as load_xiaojie_session_reference,
    persist_trade_limit_rows as persist_xiaojie_trade_limit_rows,
    trade_limits as read_xiaojie_trade_limits,
)
from .xiaojie_outcome_settlement import settle_session as settle_xiaojie_session
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
    MODEL_VERSION as WATCHLIST_MAIN_WAVE_MODEL_VERSION,
    STRATEGY_KEY as WATCHLIST_MAIN_WAVE_STRATEGY_KEY,
    latest_shadow_priors_v2,
    main_wave_v2_shadow_signal,
    run_watchlist_main_wave_v2_research,
)
from .watchlist_countertrend_rebound import (
    MODEL_VERSION as WATCHLIST_REBOUND_MODEL_VERSION,
    STRATEGY_KEY as WATCHLIST_REBOUND_STRATEGY_KEY,
    countertrend_rebound_failure_reduce_signal,
    countertrend_rebound_realtime_signal,
    latest_rebound_priors,
    run_countertrend_rebound_research,
)
from .watchlist_shadow_research_runtime import (
    WatchlistShadowResearchRuntime,
    WatchlistShadowResearchRuntimeDependencies,
)
from .intraday_decision_context import (
    decision_context as intraday_decision_context,
    invalidate_intraday_probability_profiles,
    load_intraday_probability_profiles,
    probability_for_signal as intraday_probability_for_signal,
)
from .feature_snapshot_repository import materialize_feature_snapshot
from .feature_snapshot_runtime import FeatureSnapshotRuntime, FeatureSnapshotRuntimeDependencies
from .provider_control_plane_runtime import (
    ProviderControlPlaneRuntime,
    ProviderControlPlaneRuntimeDependencies,
    mirror_runtime_rate_limits,
)
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
from .post_close_evidence_repository import load_exact_board_context_rows, load_tushare_lhb_context_rows
from .limit_pool_merge import merge_limit_pool_sources as merge_persisted_limit_pool_sources
from .strategy_pattern_sample_repository import (
    load_strategy_pattern_sample_inputs,
    persist_strategy_pattern_run as persist_strategy_pattern_run_isolated,
)
from .strategy_pattern_mining_service import (
    StrategyPatternMiningDependencies,
    run_strategy_pattern_mining as run_strategy_pattern_mining_isolated,
)
from .ten_day_leader_ranking import rank_ten_day_candidates
from .ten_day_leader_rotation_research import (
    MODEL_VERSION as TEN_DAY_LEADER_ROTATION_MODEL_VERSION,
    classify_ten_day_coordination,
)
from .ten_day_leader_rotation_repository import (
    completed_for_date as ten_day_leader_rotation_completed_for_date,
    latest_full_market_date as latest_ten_day_full_market_date,
    load_ten_day_ranking_inputs,
    persist_ten_day_rotation_run,
)
from .ten_day_leader_rotation_service import (
    TenDayLeaderRotationDependencies,
    run_ten_day_leader_rotation as run_ten_day_leader_rotation_isolated,
)
from .ten_day_leader_rotation_scheduler import (
    post_close_materialization_window as ten_day_leader_rotation_ready_window,
    ten_day_leader_rotation_scheduler,
)
from .ten_day_leader_rotation_runtime import (
    TenDayLeaderRotationRuntimeDependencies,
    run_ten_day_leader_rotation_loop as run_ten_day_leader_rotation_runtime_loop,
)
from .ten_day_leader_rotation_intraday_research import (
    evaluate_intraday_rotation_candidates,
    intraday_rotation_due as ten_day_leader_rotation_intraday_due,
    select_intraday_rotation_slice,
)
from .ten_day_leader_rotation_intraday_repository import (
    persist_intraday_rotation_observations,
    persist_intraday_rotation_scan_status,
)
from .ten_day_leader_rotation_intraday_service import (
    TenDayLeaderRotationIntradayDependencies,
    persist_ten_day_leader_rotation_intraday,
)
from .post_close_strategy_service import (
    candidates as persisted_post_close_strategy_candidates,
    completed_for_date as persisted_post_close_strategy_completed_for_date,
    retry_window as post_close_strategy_retry_window,
    run as persisted_run_post_close_strategy,
)
from .post_close_scheduler import post_close_strategy_scheduler
from .strategy_review_scheduler import strategy_review_scheduler
from .strategy_runtime_runners import (
    PostCloseStrategyRuntimeDependencies,
    StrategyReviewRuntimeDependencies,
    persist_strategy_review as persist_strategy_review_runtime,
    run_post_close_strategy_loop as run_post_close_strategy_runtime_loop,
    run_strategy_review_loop as run_strategy_review_runtime_loop,
)
from .analyst_market_review import build_recorded_analyst_market_review
from .intraday_outcome_settlement import settle as persist_intraday_outcome_settlement
from .intraday_outcome_runtime import IntradayOutcomeRuntime, IntradayOutcomeRuntimeDependencies
from .tushare_normalization import normalize_rows as pure_normalize_tushare_rows
from .market_regimes import (
    STRATEGY_INDEX_SYMBOLS,
    strategy_index_regime as pure_strategy_index_regime,
    strategy_market_regime as pure_strategy_market_regime,
    strategy_market_state as pure_strategy_market_state,
    strategy_rank as pure_strategy_rank,
)
from .strategy_index_sync import sync_index_context as sync_index_context_isolated
from .settled_limit_pool_repository import persist_settled_limit_pool
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
    tencent_index_daily,
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
from .market_flow_repository import (
    persist_intraday_market_flow_feature,
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
from .limit_linkage_mining_service import LimitLinkageMiningDependencies, run as run_limit_linkage_mining_isolated
from .async_limit_linkage_relation_repository import relations as read_async_limit_linkage_relations
from .async_board_rotation_outbox_repository import suppress_legacy_deliveries as suppress_async_legacy_board_rotation_deliveries
from . import intraday_evidence_read_model as intraday_evidence_reads
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
from .market_event_capture import capture as capture_market_events
from .market_event_runtime import run_market_event_capture_loop
from .level1_snapshot_runtime import capture_level1_snapshot, run_level1_snapshot_loop
from .intraday_fast_quote_service import cross_source_confirmation, run_intraday_fast_quote_loop
from .intraday_fast_quote_runtime import (
    IntradayFastQuoteRuntimeDependencies,
    run_intraday_fast_quote_runtime_loop,
)
from .intraday_fast_quote_confirmation_runtime import latest_confirmations as latest_fast_quote_confirmations
from .study_realtime import looks_like_response_header, realtime_rows_are_current
from .provider_health import (
    provider_error_availability,
    record_provider_api_capability,
    record_provider_empty_result,
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
    apply_background_runtime_profile, background_runtime_profile,
    background_tasks_enabled, observe_completed_task, start_leased_background_tasks,
    supervise_leased_loop, validate_runtime_task_specs,
)
from .platform.runtime_task_registry import (
    runtime_profile_owns_task, runtime_task_contract, runtime_task_contract_catalog,
)
from .platform.strategy_registry import validate_strategy_runtime_versions
from .runtime_composition import (
    LeasedRuntimeDependencies,
    build_leased_task_runner,
    current_lease_fence,
    lease_key_for_label,
)
from .application_lifecycle import ApplicationLifecycleDependencies, application_lifespan
from .background_task_catalog import build_specs as build_background_task_specs
from .logging_config import get_logger
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
from .edge_evidence_transfer import (
    JOURNAL_RETENTION_DAYS as EDGE_CHANGE_JOURNAL_RETENTION_DAYS,
    prune_change_journal as prune_edge_change_journal,
)
from .market_session_repository import (
    realtime_market_session as read_realtime_market_session,
    sse_calendar_status as read_sse_calendar_status,
)
from .intraday_signal_policy import (
    signal_event_state as intraday_signal_event_state,
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
from .edge_evidence_transfer import read_live_session_acceptance
from .research_storage_admission import ResearchStorageAdmission, governance as research_storage_governance_isolated
from .health_read_model import DatabaseUnavailableError, HealthDependencies, health_payload as read_health_payload
from .release_metadata import release_metadata
from .replay_readiness import historical_replay_readiness
from . import research_capacity
from .feature_read_repository import analyst_feature as read_analyst_feature
from .feature_read_repository import latest_tushare_row as read_latest_tushare_row
from .feature_read_repository import market_regime as read_market_regime
from .analyst_text_features import DEFAULT_FACTOR_VERSION, analyst_text_factor_summary as read_analyst_text_factor_summary
from .stock_study_readiness_repository import (
    stock_study_claims as read_stock_study_claims,
    stock_window_readiness as read_stock_window_readiness,
)
from .intraday_status_read_model import IntradayStatusDependencies, intraday_services_status_payload as read_intraday_services_status_payload, intraday_services_status_payload_async as read_intraday_services_status_payload_async
from .routers.provider_status import build_provider_status_router
from .routers.longhu_reads import build_longhu_reads_router
from .routers.licensed_stock_api import build_licensed_stock_api_router
from .routers.research_readiness import build_research_readiness_router
from .routers.intraday_status import build_intraday_status_router
from .routers.analyst_reads import build_analyst_reads_router
from .routers.analyst_trade_action_reads import build_analyst_trade_action_reads_router
from .routers.analyst_action_outcomes import build_analyst_action_outcomes_router
from .routers.analyst_skill_reads import build_analyst_skill_reads_router
from .routers.analyst_research_reads import build_analyst_research_reads_router
from .routers.automation_reads import build_automation_reads_router
from .security import licensed_stock_read_allowed, remote_archive_sync_bearer_allowed, write_access_allowed
from .symbols import canonical_symbol
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
from .routers.personal_decisions import PersonalDecisionDependencies, build_personal_decisions_router
from .routers.analyst_prompt_lab import build_analyst_prompt_lab_router
from .routers.strategy_pattern_reads import build_strategy_pattern_reads_router
from .routers.ten_day_leader_rotation_reads import build_ten_day_leader_rotation_reads_router
from .routers.ten_day_leader_rotation_actions import (
    TenDayLeaderRotationActionDependencies,
    build_ten_day_leader_rotation_actions_router,
)
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
from .routers.l2_research import L2ResearchDependencies, build_l2_research_router
from .routers.provider_actions import ProviderActionDependencies, build_provider_actions_router
from .routers.market_actions import MarketActionDependencies, build_market_actions_router
from .routers.intraday_actions import IntradayActionDependencies, build_intraday_actions_router
from .routers.sector_actions import SectorActionDependencies, build_sector_actions_router
from .routers.strategy_actions import StrategyActionDependencies, build_strategy_actions_router
from .routers.xiaojie_leader_flow import build_xiaojie_leader_flow_router
from .routers.research_actions import ResearchActionDependencies, build_research_actions_router
from .routers.ingestion_actions import IngestionActionDependencies, build_ingestion_actions_router
from .routers.system_control import SystemControlDependencies, build_system_control_router
from .market_rules import a_share_limit_ratio, china_equity_session, china_futures_session, cn_today, is_st_security_name  # noqa: F401 -- re-exported for tests/provider_test_support.py
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
    FuyaoQueryRequest,
    FullMarketDailyControlsSyncRequest,
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
    L2IncrementalEvaluationRequest,
    StrategyDecisionRequest,
    StrategyPatternMiningRequest,
    StrategyReviewRequest,
    WatchlistMainWaveResearchRequest,
    TushareFetchRequest,
    TushareCapabilityAuditRequest,
    TushareSyncRequest,
    UniverseUpdateRequest,
)
from .ten_day_leader_rotation_contracts import TenDayLeaderRotationRunRequest
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
from .intraday_replay_service import (
    IntradayEntryTimingChallengerDependencies,
    IntradayReplayDependencies,
)
from . import intraday_replay_service
from .post_close_refresh import record_stage_with_receipt, run_refresh as run_post_close_refresh_orchestrated
from .post_close_refresh_runtime import PostCloseRefreshRuntime
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
from .longhu_market_service import sync as sync_longhu_full_market_close
from .longhu_market_repository import persisted_close_context as read_longhu_close_context
from .longhu_vendor_source import (
    configured as longhu_vendor_configured,
    intraday_source as longhu_intraday_source,
)
from .minute_bar_session_backfill import (
    backfill_session as backfill_minute_session,
    session_symbols as session_minute_symbols,
)
from .earnings_calendar_sync import sync as sync_earnings_calendar_isolated
from .stock_money_flow_sync import persist_flow_rows as persist_stock_money_flow_rows
from .stock_money_flow_sync import sync as sync_stock_money_flow_isolated
from .disclosure_day_watch import MODEL_VERSION as DISCLOSURE_DAY_WATCH_MODEL_VERSION
from .limit_up_continuation import MODEL_VERSION as LIMIT_UP_CONTINUATION_MODEL_VERSION
from .core_daily_control_sync import CoreDailyControlDependencies, sync as sync_core_daily_controls_isolated
from .sector_catalog_sync import sync_all as sync_all_sector_catalogs_isolated
from .ths_sector_catalog_sync import sync as sync_ths_sector_catalog_isolated
from .eastmoney_sector_members_sync import sync as sync_eastmoney_sector_members_isolated
from .eastmoney_live_hydration import hydrate as hydrate_eastmoney_live_isolated
from .ths_sector_flows import sync_industry as sync_ths_industry_isolated, sync_concept_signals as sync_ths_concept_signals_isolated
from .outcome_recomputation import recompute as recompute_outcomes_isolated
from .post_close_candidate_outcomes import settle_post_close_and_leader_rotation_outcomes
from .market_regime_daily import materialize_market_regime
from .sentiment_cycle_daily import materialize_sentiment_cycle
from .strategy_daily_candidate_ledger import materialize_ledger, settle_ledger_outcomes as settle_strategy_ledger_outcomes
from .watchlist_candidate_proposals import materialize_watchlist_proposals
from .ths_concept_members_sync import sync as sync_ths_concept_members_isolated
from .analyst_scorecards import readiness as analyst_scorecard_readiness
from .analyst_scorecards import recompute as recompute_scorecards_isolated
from .claim_review_service import review_claim as review_claim_isolated
from .analyst_trade_action_read_model import anqiang_trade_action_replay
from .analyst_skill_models import analyst_skill_profiles
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
from .l2_research_gate import evaluate_l2_incremental_value
from .l2_research_repository import latest_l2_evaluation, persist_l2_evaluation
from .personal_decision_repository import persist_broker_snapshot, persist_trade_plan
from .async_personal_decision_repository import (
    latest_broker_snapshot,
    latest_decision_research,
    latest_personal_decision_brief,
)
from .decision_research_service import refresh_decision_research_and_plans
from .provider_rate_limits import provider_request_spacing_seconds, reserve_provider_rate_limit_slot
from .runtime_leases import (
    POST_CLOSE_REFRESH_LEASE_KEY,
    acquire_runtime_lease,
    background_loop_lease_seconds,
    check_runtime_lease_fence,
    post_close_refresh_lease_seconds,
    release_runtime_lease,
    renew_runtime_lease,
)
from .tushare_catalog import CORE_NORMALIZED_APIS, TUSHARE_CATALOG, catalog_items
from .tushare_catalog_fetch_service import CatalogFetchDependencies, fetch_catalog as run_catalog_fetch
from .stock_study_tushare_service import StockStudyTushareDependencies, fetch_stock_study_input
from .stock_study_service import StockStudyDependencies, build as build_stock_study_isolated
from .stock_study_public_service import StockStudyPublicDependencies, fetch as fetch_stock_study_public
from .intraday_signal_generation import IntradaySignalGenerationDependencies, generate_intraday_signals
from .intraday_signal_event_persistence import (
    IntradaySignalEventPersistenceDependencies,
    persist_generated_signals,
)
from .intraday_rule_input_retention_runtime import (
    IntradayRuleInputRetentionDependencies,
    IntradayRuleInputRetentionRuntime,
)
from .intraday_scan_preparation import (
    IntradayScanPreparationDependencies,
    prepare_intraday_scan_inputs,
)
from .intraday_scan_source_status import build_scan_source_status
from .intraday_scan_signal_persistence import (
    IntradayScanPersistenceServiceDependencies,
    IntradayScanSignalPersistenceDependencies,
)
from .intraday_scan_persistence_runtime import IntradayScanPersistenceRuntime
from .intraday_watch_quote_capture import WatchQuoteCaptureDependencies
from .intraday_watchlist_scan_runtime import (
    IntradayWatchlistScanRuntime,
    IntradayWatchlistScanRuntimeDependencies,
)
from .intraday_watchlist_scan_service import run_watchlist_scan
from .all_board_member_backfill_service import (
    AllBoardMemberBackfillDependencies,
    run as run_all_board_member_backfill_isolated,
)
from .all_board_member_backfill_runtime import (
    AllBoardMemberBackfillLoopDependencies,
    all_board_member_backfill_loop as run_all_board_member_backfill_loop_runtime,
)
from .ths_concept_member_backfill_service import (
    ThsConceptMemberBackfillDependencies,
    run as run_ths_concept_member_backfill_isolated,
)
from .ths_concept_member_backfill_runtime import (
    ThsConceptMemberBackfillLoopDependencies,
    ths_concept_member_backfill_loop as run_ths_concept_member_backfill_loop_runtime,
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
    HISTORICAL_MINUTE_APIS,
    REALTIME_MARKET_HOURS_APIS,
    default_probe_params,
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


logger = get_logger(__name__)
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
post_close_refresh_runtime = PostCloseRefreshRuntime()


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
    return Settings.from_environ(environ).legacy_schema_bootstrap_enabled


def control_plane_writes_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Peer/edge deployments read a shared control plane and must not write to it.

    Defaults to enabled so the primary deployment keeps registering catalog
    capabilities at startup exactly as before; a peer compose profile sets
    this to ``false`` to skip that write path entirely.
    """
    return Settings.from_environ(environ).control_plane_writes_enabled


def provider_global_rate_limit_max_wait_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Keep shared provider reservations bounded so callers fail locally first."""
    return Settings.from_environ(environ).provider_global_rate_limit_max_wait_seconds


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
    values = requested or list(Settings.from_environ().quant_universe)
    if not values:
        values = read_sync_core_symbols(db)
    if not values:
        values = read_sync_analyst_claim_symbols(db)
    return _normalize_sync_symbols(values)


async def resolve_sync_symbols_async(requested: list[str]) -> list[str]:
    """Resolve the same bounded universe without blocking an async caller."""
    values = requested or list(Settings.from_environ().quant_universe)
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
    ProviderControlPlaneRuntime(ProviderControlPlaneRuntimeDependencies(
        database=db,
        provider_configs=provider_configs,
        catalog_items=catalog_items,
        capability_contract=api_capability,
        super_get_verified_apis=SUPER_GET_VERIFIED_APIS,
        json_value=Json,
    )).initialize()


def sync_runtime_provider_rate_limits(connection: Any, configs: Mapping[str, Any] | None = None) -> None:
    """Mirror the effective Tushare limiter configuration into the read-only control plane.

    Environment configuration is the one runtime source of truth because the
    limiter is process-local and takes effect at startup.  Keeping this small
    mirror current avoids a stale database rate appearing in the UI as if it
    governed live requests; no credentials or endpoint details are stored.
    """
    mirror_runtime_rate_limits(connection, provider_configs() if configs is None else configs)


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
    try:
        return FeatureSnapshotRuntime(FeatureSnapshotRuntimeDependencies(
            database=db,
            materialize=materialize_feature_snapshot,
            feature_version=FEATURE_VERSION,
            number=number,
            market_regime=market_regime,
            analyst_text_factor_summary=analyst_text_factor_summary,
            latest_tushare_row=latest_tushare_row,
            analyst_feature=analyst_feature,
        )).build(as_of_date, universe_key)
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


def _intraday_signal_attribution_refresh_dependencies() -> IntradaySignalAttributionRefreshDependencies:
    return IntradaySignalAttributionRefreshDependencies(
        attribution_for=intraday_signal_attribution, json_safe=strategy_json_safe,
    )


def refresh_intraday_signal_attributions(connection: Any, *, cutoff: datetime) -> int:
    """Compatibility export backed by the intraday signal attribution service."""
    return intraday_signal_attribution_service.refresh_intraday_signal_attributions(
        connection, cutoff=cutoff, dependencies=_intraday_signal_attribution_refresh_dependencies(),
    )


def recompute_intraday_signal_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    """Settle confirmed alerts from persisted evidence through the shared repository."""
    result = IntradayOutcomeRuntime(IntradayOutcomeRuntimeDependencies(
        database=db,
        outcome_cutoff=intraday_outcome_cutoff,
        refresh_attributions=refresh_intraday_signal_attributions,
        settle=persist_intraday_outcome_settlement,
        horizons=INTRADAY_OUTCOME_HORIZONS,
        direction_for=intraday_signal_direction,
        metrics_for=intraday_signal_outcome_metrics,
        decimal_or_none=decimal_or_none,
        barrier_spec_type=LabelSpec,
        triple_barrier_label=triple_barrier_label,
        persist_barrier_outcome=persist_barrier_outcome,
        return_decomposition=a_share_return_decomposition,
        json_safe=strategy_json_safe,
    )).recompute(as_of_date)
    invalidate_intraday_probability_profiles()
    return result


def recompute_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    """Compatibility entry point backed by local-only outcome recomputation."""
    return recompute_outcomes_isolated(
        as_of_date,
        cn_today=cn_today,
        db=db,
        recompute_intraday_signal_outcomes=recompute_intraday_signal_outcomes,
        settle_post_close_and_leader_rotation_outcomes=settle_post_close_and_leader_rotation_outcomes,
        settle_ledger_outcomes=settle_strategy_ledger_outcomes,
    )


def materialize_market_regime_today(as_of_date: date) -> dict[str, Any]:
    """Persist the multi-index regime label for one already-closed trading day."""
    with db.transaction() as connection:
        return materialize_market_regime(connection, as_of_date)


def materialize_sentiment_cycle_today(as_of_date: date) -> dict[str, Any]:
    """Persist the short-term board-tape reading for one already-closed session."""
    with db.transaction() as connection:
        return materialize_sentiment_cycle(connection, as_of_date)


def materialize_strategy_daily_candidate_ledger(as_of_date: date) -> dict[str, int]:
    """Normalize whatever each strategy's own table already holds for as_of_date into the ledger."""
    with db.transaction() as connection:
        return materialize_ledger(connection, as_of_date)


def materialize_daily_watchlist_proposals(as_of_date: date) -> dict[str, Any]:
    """Read-only daily proposal list; never writes into intraday_watchlists."""
    with db.transaction() as connection:
        return materialize_watchlist_proposals(connection, as_of_date)


def settle_xiaojie_leader_flow_outcomes(as_of_date: date) -> dict[str, Any]:
    """Attach realised outcomes to one session's leader-flow observations."""
    with db.transaction() as connection:
        return settle_xiaojie_session(connection, as_of_date)


def _read_session_minute_symbols(as_of_date: date) -> dict[str, Any]:
    """Read one session's board + benchmark symbol list off the executor."""
    with db.transaction() as connection:
        return session_minute_symbols(connection, as_of_date)


async def backfill_session_minute_bars(as_of_date: date) -> dict[str, Any]:
    """Minute bars for one session's boards and benchmarks (research-only).

    Gathered last in the post-close pipeline because ``stk_mins`` is a slow,
    per-symbol route: it answered ~55% of sampled boards over three closed
    sessions and 0% intraday, so this is best-effort supplementary data.
    ``availability_pct`` rides out in the result so a low-answer night reads as
    low availability rather than an empty table, and a re-run backfills the
    rest since the write is idempotent.  The symbol read is offloaded like
    every other database call an async path makes, so the event loop is never
    blocked on a sync transaction.
    """
    selection = await run_database_blocking(
        lambda: _read_session_minute_symbols(as_of_date), timeout_seconds=30)
    return await backfill_minute_session(
        as_of_date, symbols=selection["symbols"], call_tushare_api=call_tushare_api,
        run_database_blocking=run_database_blocking, db=db)


async def sync_stock_money_flow(trade_date: date) -> dict[str, Any]:
    """Ingest one completed session's per-stock capital flow (end-of-day only)."""
    return await sync_stock_money_flow_isolated(
        trade_date, call_tushare_api=call_tushare_api, parse_date=tushare_date,
        expected_symbols=full_market_daily_row_count, run_database_blocking=run_database_blocking,
        db=db, safe_error_detail=safe_error_detail,
    )


async def sync_earnings_calendar(as_of_date: date) -> dict[str, Any]:
    """Ingest one reporting period's disclosure calendar and prior guidance."""
    return await sync_earnings_calendar_isolated(
        as_of_date, call_tushare_api=call_tushare_api, parse_date=tushare_date,
        run_database_blocking=run_database_blocking, db=db, safe_error_detail=safe_error_detail,
    )


def generate_recommendations(request: GenerateRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated scorer/materializer."""
    return generate_recommendations_isolated(
        request, cn_today=cn_today, build_feature_snapshot=build_feature_snapshot,
        analyst_execution_context=analyst_execution_context, ablation_scores=ablation_scores,
        number=number, db=db, model_version=MODEL_VERSION, feature_version=FEATURE_VERSION,
        json_safe=strategy_json_safe,
    )


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
    """Persist raw API evidence before promoting the supported canonical subset.

    A full A-share cross-section is several thousand rows; this used to be
    one INSERT per row (wp10-report.md "需要他人处理" #4). The raw evidence
    write is now one batched ``INSERT ... SELECT FROM unnest(...)``. Rows are
    deduplicated client-side by their conflict key first: a single INSERT
    statement cannot target the same
    ``(provider_key,api_name,record_key,content_sha256)`` row twice, which a
    sequential loop tolerated (the second call just overwrote the first).
    """
    if rows:
        deduped: dict[tuple[str, str], tuple[int, str]] = {}
        for index, row in enumerate(rows):
            serialized_for_hash = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            content_hash = hashlib.sha256(serialized_for_hash.encode()).hexdigest()
            record_key = tushare_record_key(row, request_key, index)
            row_json = json.dumps(row, ensure_ascii=False, default=str)
            # Last occurrence wins, matching the previous sequential
            # behaviour where a later duplicate's ON CONFLICT UPDATE
            # overwrote an earlier one's.
            deduped[(record_key, content_hash)] = (index, row_json)
        record_indexes = [item[0] for item in deduped.values()]
        record_keys = [key[0] for key in deduped]
        content_hashes = [key[1] for key in deduped]
        row_jsons = [item[1] for item in deduped.values()]
        connection.execute(
            """INSERT INTO quant.tushare_raw_records(provider_key,api_name,request_key,record_index,record_key,content_sha256,row_data,available_at)
               SELECT %s,%s,%s,t.record_index,t.record_key,t.content_sha256,t.row_data::jsonb,%s
                 FROM unnest(%s::integer[],%s::text[],%s::text[],%s::text[])
                      AS t(record_index,record_key,content_sha256,row_data)
               ON CONFLICT(provider_key,api_name,record_key,content_sha256) DO UPDATE SET available_at=EXCLUDED.available_at,request_key=EXCLUDED.request_key""",
            (provider_key, api_name, request_key, available_at,
             record_indexes, record_keys, content_hashes, row_jsons),
        )
    return normalize_tushare_rows(connection, api_name, rows, available_at, provider_key)


async def sync_market_universe(request: MarketUniverseSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by the isolated universe synchronizer."""
    if request.provider == "auto" and longhu_vendor_configured():
        result = await sync_longhu_full_market_close(
            cn_today(), db=db, run_public_blocking=run_akshare_blocking,
            run_database_blocking=run_database_blocking, persist_rows=persist_tushare_rows,
            persist_flow_rows=persist_stock_money_flow_rows,
        )
        return {**result, "universe_key": request.universe_key,
                "members": int(result.get("daily_rows") or result.get("imported") or 0)}
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


async def sync_full_market_daily(request: FullMarketDailySyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated full-market sync."""
    if request.provider == "auto" and longhu_vendor_configured():
        return await sync_longhu_full_market_close(
            request.trade_date or cn_today(), db=db, run_public_blocking=run_akshare_blocking,
            run_database_blocking=run_database_blocking, persist_rows=persist_tushare_rows,
            persist_flow_rows=persist_stock_money_flow_rows,
        )
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
        record_provider_empty=record_provider_empty_result,
    )


def full_market_daily_row_count(trade_date: date) -> int:
    """Compatibility export backed by the daily control-plane module."""
    return daily_control_plane.daily_row_count(db, trade_date)


def full_market_daily_control_status() -> dict[str, Any]:
    """Expose latest daily control coverage without requesting a provider."""
    with db.transaction() as connection:
        row = connection.execute(EQUITY_DAILY_CONTROL_STATUS_SQL).fetchone()
    return daily_control_plane_status_payload(row)


def _daily_control_plane_sync_dependencies() -> DailyControlPlaneSyncDependencies:
    return DailyControlPlaneSyncDependencies(
        database=db, longhu_vendor_configured=longhu_vendor_configured, run_database=run_database_blocking,
        call_tushare_api=call_tushare_api, parse_tushare_date=tushare_date, persist_tushare_rows=persist_tushare_rows,
        persist_blocked=persist_tushare_fetch_blocked, safe_error_detail=safe_error_detail,
        executor_saturated_error=ExecutorSaturatedError, record_provider_success=record_provider_success,
        record_provider_failure=record_provider_failure, record_provider_api_capability=record_provider_api_capability,
    )


async def sync_full_market_daily_controls(trade_date: date) -> dict[str, Any]:
    """Compatibility export backed by the daily control-plane module."""
    return await daily_control_plane.sync_full_market_daily_controls(
        trade_date, _daily_control_plane_sync_dependencies(),
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
    """Persist one complete response without inventing a historical start date."""
    return persist_ths_sector_snapshot(
        connection, taxonomy_key, sector_key, rows, provider_key, available_at,
        ensure_instrument=ensure_tushare_instrument, parse_date=tushare_date,
    )


def eastmoney_member_symbol(row: dict[str, Any]) -> str | None:
    """Normalize the public board constituent code without guessing exchanges."""
    code = str(row.get("代码") or row.get("code") or row.get("股票代码") or "").strip().upper()
    return canonical_symbol(code, kind="stock")


def persist_eastmoney_sector_members(connection: Any, taxonomy_key: str, sector_key: str, rows: list[dict[str, Any]],
                                     available_at: datetime) -> int:
    """Persist a current-snapshot response with its real observation date."""
    def ensure_instrument(connection: Any, symbol: str, row: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO quant.instruments(symbol,exchange,name,source) VALUES(%s,%s,%s,'akshare') "
            "ON CONFLICT(symbol) DO UPDATE SET name=coalesce(EXCLUDED.name,quant.instruments.name),updated_at=now()",
            (symbol, exchange_for(symbol), str(row.get("名称") or row.get("name") or "").strip() or None),
        )

    return persist_observed_sector_snapshot(
        connection, taxonomy_key, sector_key, rows, "akshare", available_at,
        member_symbol=eastmoney_member_symbol, ensure_instrument=ensure_instrument,
    )


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
    """Return board flow with documented Fuyao all-A price/turnover leaders."""
    result = await run_intraday_sector_report_isolated(
        request,
        run_public_blocking=run_akshare_blocking,
        board_flow=akshare_eastmoney_board_flow,
        all_a_snapshot=fuyao_all_a_snapshot_rows,
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


INTRADAY_SIGNAL_MODEL_VERSION = "watchlist-confirmation-v6"
INTRADAY_CONFIRMATION_WINDOW = timedelta(minutes=5)
INTRADAY_ALERT_COOLDOWN = timedelta(minutes=10)
INTRADAY_ALERT_MAX_ATTEMPTS = 3
# This process-local cache contains only the current explicit watch/peer
# basket.  Entries expire quickly and are pruned in ``intraday_tencent_surge_context``.
_intraday_tencent_minute_cache: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}
_intraday_longhu_minute_cache: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}
INTRADAY_ALL_A_SNAPSHOT_TTL_SECONDS = 30.0


async def _fetch_intraday_all_a_snapshot_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch the documented Fuyao/THS cross-section without a Tencent all-A call."""
    return await fuyao_all_a_snapshot_rows()


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
    """Return one shared, explicitly aged Fuyao all-A price/turnover snapshot."""
    (rows, supplier_status), cache_status = await _intraday_all_a_snapshots.get()
    return rows, {**supplier_status, **cache_status}


def merge_intraday_watch_quote_prices(quotes: dict[str, dict[str, Any]], depth_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Overlay fresh batched watch prices without inventing flow fields."""
    return merge_intraday_watch_quote_prices_pure(quotes, depth_rows, number=intraday_number)


def merge_intraday_longhu_watch_quotes(
    quotes: dict[str, dict[str, Any]], rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Prefer a valid licensed quote without changing unrelated flow fields."""
    return merge_intraday_longhu_watch_quotes_pure(quotes, rows, number=intraday_number)


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


def intraday_quote_from_fuyao(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize the documented THS all-A quote without inventing fund flow."""
    return intraday_quote_from_fuyao_pure(row)


def annotate_intraday_flow_percentiles(quotes: dict[str, dict[str, Any]]) -> None:
    """Attach a cross-sectional main-flow percentile without assuming units.

    Tencent's public flow unit is provider-specific, so extreme buy/sell is
    judged against the same all-A snapshot instead of a fragile absolute yuan
    threshold.  This is the cross-sectional normalization pattern used by
    factor research systems, applied only to the observed universe.
    """
    annotate_intraday_flow_percentiles_pure(quotes)


async def intraday_watch_flow_reference(
    symbols: list[str], observed_at: datetime,
) -> dict[str, dict[str, Any]]:
    """Read the local float-share and trailing-volume reference for one scan."""
    return await read_async_watch_flow_reference(async_db, symbols, observed_at)


#: One session's reference is reloaded only when the trading date rolls over.
_xiaojie_session_reference: dict[str, Any] = {"trading_date": None, "reference": None}
#: Per-session MA5-break timers, reset when the trading date rolls over.
_xiaojie_ma5_break_state: dict[str, Any] = {}
_launch_velocity_state: dict[str, Any] = {}


async def _xiaojie_session_context(trading_date: date) -> dict[str, Any]:
    """Load and cache one session's limits, memberships and prior-bar reference."""
    cached = _xiaojie_session_reference
    if cached["trading_date"] == trading_date and cached["reference"] is not None:
        return cached["reference"]

    async def read_limits(day: date) -> dict[str, float]:
        return await run_database_blocking(
            lambda: _with_connection(lambda connection: read_xiaojie_trade_limits(connection, day)),
        )

    async def persist_limits(day: date, rows: list[dict[str, Any]]) -> int:
        return await run_database_blocking(
            lambda: _with_connection(lambda connection: persist_xiaojie_trade_limit_rows(
                connection, day, rows, "tushare", datetime.now(timezone.utc))),
            timeout_seconds=180,
        )

    # Limit prices are published pre-open but only land in the table after the
    # close, so intraday they must be provisioned before anything reads them.
    await ensure_xiaojie_session_trade_limits(
        trading_date, read_limits=read_limits, call_tushare_api=call_tushare_api,
        persist_limits=persist_limits,
    )
    reference = await run_database_blocking(
        lambda: _with_connection(lambda connection: load_xiaojie_session_reference(connection, trading_date)),
        timeout_seconds=180,
    )
    _xiaojie_session_reference.update({"trading_date": trading_date, "reference": reference})
    _xiaojie_ma5_break_state.clear()
    _launch_velocity_state.clear()
    return reference


def _with_connection(action: Any) -> Any:
    with db.transaction() as connection:
        return action(connection)


def _xiaojie_leader_flow_dependencies() -> XiaojieLeaderFlowDependencies:
    return XiaojieLeaderFlowDependencies(
        database=db, run_database=run_database_blocking, session_context=_xiaojie_session_context,
        ma5_break_state=_xiaojie_ma5_break_state, velocity_state=_launch_velocity_state,
        deliver_alert=deliver_intraday_alert, alert_text=_xiaojie_alert_text, safe_error_detail=safe_error_detail,
    )


async def run_xiaojie_leader_flow(*, scan_id: uuid.UUID, observed_at: datetime,
                                  all_a_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the leader pool from this scan's own cross-section.

    Research-only throughout: the emitted signal events carry a dedicated
    stage so nothing on the decision path can mistake them for watchlist
    alerts, and the strategy stays at zero live weight in the promotion
    registry.
    """
    return await xiaojie_leader_flow_runtime_run(
        scan_id=scan_id, observed_at=observed_at, all_a_rows=all_a_rows,
        dependencies=_xiaojie_leader_flow_dependencies(),
    )


def _persist_xiaojie_signal_event(connection: Any, scan_id: uuid.UUID, observed_at: datetime,
                                  candidate: dict[str, Any]) -> uuid.UUID:
    """Compatibility export backed by the xiaojie leader-flow runtime module."""
    return persist_xiaojie_signal_event(connection, scan_id, observed_at, candidate)


def _xiaojie_alert_name(symbol: str, names: Mapping[str, str] | None) -> str:
    """Label a symbol the way the person reading the alert recognises it.

    A live snapshot carries no name, so an alert used to name a stock by code
    alone.  The name leads because that is what a phone notification is read
    by; the code follows so it stays copy-pasteable.  An unnamed symbol - a
    fresh listing that has not reached ``instruments`` yet - degrades to the
    bare code rather than failing the alert.
    """
    name = (names or {}).get(symbol)
    return f"{name} {symbol}" if name else symbol


def _xiaojie_alert_text(candidate: dict[str, Any], trading_date: date,
                        names: Mapping[str, str] | None = None) -> str:
    evidence = candidate.get("evidence") or {}
    board = evidence.get("board") or {}
    state = "封板" if board.get("sealed") else ("炸板" if board.get("broken") else "近板")
    pct = evidence.get("pct_change")
    label = _xiaojie_alert_name(candidate["symbol"], names)
    return (
        f"【研究观察·小杰龙头】{label} {candidate.get('mode')}\n"
        f"{trading_date} {state} 涨幅 {pct:.2f}%\n" if pct is not None else
        f"【研究观察·小杰龙头】{label} {candidate.get('mode')}\n{trading_date} {state}\n"
    ) + (
        f"研究仓位参考 {(candidate.get('position') or {}).get('target_fraction')}；"
        f"风险标记 {', '.join(candidate.get('risk_flags') or []) or '无'}\n"
        "仅为研究观察，零实盘权重，不构成交易指令。"
    )


async def intraday_watch_volume_fallback(symbols: list[str]) -> dict[str, float]:
    """Batched live cumulative volume, used only when the all-A snapshot fails.

    ProMax ``rt_k`` answers the whole watch basket in one request with a
    second-resolution ``updated_at``, so it is an independent third source for
    the one input the derived flow metrics need.  It supplies volume only; the
    decision price still comes from the Tencent batch.
    """
    if not symbols:
        return {}
    call = await call_tushare_api("rt_k", {"ts_code": ",".join(symbols)}, None, "super_get")
    volumes: dict[str, float] = {}
    for row in call.rows:
        symbol = str(row.get("ts_code") or "").upper()
        volume = intraday_number(row.get("vol"))
        if symbol in set(symbols) and volume is not None and volume > 0:
            volumes[symbol] = volume
    return volumes


def derive_intraday_watch_flow_metrics(
    quotes: dict[str, dict[str, Any]], reference: dict[str, dict[str, Any]], *, observed_at: datetime,
) -> dict[str, dict[str, float]]:
    """Derive volume ratio and turnover rate from the licensed THS snapshot.

    Both are arithmetic definitions over the snapshot's own cumulative volume,
    so this replaces the public Eastmoney watch endpoint - which fails about
    half of all 30-second scans - without adding a provider call.  It never
    derives main_net_inflow: no licensed route supplies one.
    """
    return pure_derive_watch_flow_metrics(quotes, reference, observed_at=observed_at, number=intraday_number)


def apply_intraday_derived_watch_flow_metrics(
    quotes: dict[str, dict[str, Any]], derived: dict[str, dict[str, float]],
) -> dict[str, dict[str, str]]:
    """Promote derived metrics over the public values and label every field."""
    return pure_apply_derived_watch_flow_metrics(quotes, derived)


def intraday_derived_flow_divergence(
    quotes: dict[str, dict[str, Any]], derived: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Measure derived-versus-Eastmoney agreement whenever both sources answer."""
    return pure_derived_flow_divergence(quotes, derived, number=intraday_number)


def intraday_minute_features(rows: list[dict[str, Any]], *, lookback: int = 20,
                             source: str = "tencent_free") -> dict[str, Any] | None:
    """Build a causal price/volume burst feature from normalized minute rows."""
    return pure_intraday_minute_features(rows, lookback=lookback, source=source, number=intraday_number)


def intraday_peer_context(peer_symbols: list[str], features: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Measure same-minute breadth without allowing the target into its peers."""
    return pure_intraday_peer_context(peer_symbols, features)


def post_close_exact_board_context(as_of_date: date) -> dict[str, dict[str, Any]]:
    """Join only exact THS member codes to same-date concept-flow evidence."""
    return pure_exact_board_context(
        load_exact_board_context_rows(db, as_of_date), json_safe=strategy_json_safe,
    )


def post_close_tushare_lhb_context(as_of_date: date) -> dict[str, dict[str, Any]]:
    """Aggregate deduplicated post-close institution-seat evidence by symbol."""
    return pure_lhb_context(
        load_tushare_lhb_context_rows(db, as_of_date), number=intraday_number,
    )


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


def _ten_day_leader_rotation_dependencies() -> TenDayLeaderRotationDependencies:
    """Compose the feature without moving ranking or persistence into main."""
    return TenDayLeaderRotationDependencies(
        latest_full_market_date=lambda minimum: latest_ten_day_full_market_date(db, minimum),
        load_inputs=lambda as_of_date: load_ten_day_ranking_inputs(db, as_of_date),
        rank_candidates=rank_ten_day_candidates,
        classify=classify_ten_day_coordination,
        persist=lambda **kwargs: persist_ten_day_rotation_run(db, **kwargs),
        json_safe=strategy_json_safe,
    )


def run_ten_day_leader_rotation(request: TenDayLeaderRotationRunRequest) -> dict[str, Any]:
    """Compatibility entry point for the isolated shadow materializer."""
    return run_ten_day_leader_rotation_isolated(request, _ten_day_leader_rotation_dependencies())


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


def _watchlist_history_hydration_dependencies() -> WatchlistHistoryHydrationDependencies:
    return WatchlistHistoryHydrationDependencies(
        database=db, run_database=run_database_blocking, sync_tushare=sync_tushare,
        fetch_supplemental=stock_study_fetch, daily_factors=watchlist_daily_factors, json_safe=strategy_json_safe,
    )


async def hydrate_watchlist_history(watchlist_id: uuid.UUID, symbol: str) -> dict[str, Any]:
    """Compatibility export backed by the intraday watchlist service module."""
    return await intraday_watchlist_service_hydrate_history(
        watchlist_id, symbol, _watchlist_history_hydration_dependencies(),
    )


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
    base_url = Settings.from_environ().dashboard_public_url
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
    delivery_id = await create_async_pending_intraday_alert_delivery(async_db, signal_event_id, text)
    return await attempt_intraday_alert_delivery(delivery_id, signal_event_id, text)


async def retry_pending_intraday_alerts(limit: int = 3) -> dict[str, int]:
    """Retry bounded, unsent outbox rows even when their source signal faded."""
    rows = await read_async_due_intraday_alert_deliveries(async_db, INTRADAY_ALERT_MAX_ATTEMPTS, limit)
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
    """Get a bounded, fresh minute feature window through Tushare routes."""
    async def fetch_rows(symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        source, rows = await stock_study_fetch("tushare_rt_min", TushareFetchRequest(
            api_name="rt_min", provider="super", params={"ts_code": symbol, "freq": "1MIN"}, max_rows=30, force_refresh=True,
        ))
        return source, rows

    return await fetch_bounded_minute_context(
        symbols, fetch_rows=fetch_rows, feature_builder=intraday_minute_features, number=intraday_number,
        observed_at=datetime.now(timezone.utc), max_age_seconds=90.0,
    )


def intraday_minute_profile_capture_enabled() -> bool:
    return Settings.from_environ().intraday_minute_profile_capture_enabled


def intraday_minute_profile_retention_days() -> int:
    return Settings.from_environ().intraday_minute_profile_retention_days


def intraday_minute_profile_max_symbols() -> int:
    """Bound the close capture without silently reducing the normal pool."""
    return Settings.from_environ().intraday_minute_profile_max_symbols


def intraday_longhu_max_symbols() -> int:
    """Bound licensed per-security calls independently from the watch capacity."""
    return Settings.from_environ().longhu_intraday_max_symbols


async def intraday_longhu_watch_quotes(
    symbols: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not longhu_vendor_configured():
        return [], {"status": "disabled", "requested": len(symbols), "reason": "longhu_not_configured"}

    def fetch() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return longhu_intraday_source().watch_quotes(symbols, max_symbols=intraday_longhu_max_symbols())

    return await run_akshare_blocking(fetch, timeout_seconds=8)


async def shared_longhu_quotes(
    symbols: list[str], max_symbols: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Gateway boundary for a caller-authenticated logical batch.

    ``watch_quotes`` issues one vendor request per symbol across 12 threads, so
    a full ``MAX_PAGE_SIZE`` batch of 300 symbols takes about 25 seconds of
    upstream latency alone.  A 30 second budget left roughly 5 seconds of
    headroom and turned any upstream slowdown into a 500 for the caller; 60
    seconds keeps the boundary meaningful without pushing more concurrency at
    the vendor.  Single-symbol and intraday paths keep their own tight budgets.
    """
    return await run_akshare_blocking(
        lambda: longhu_intraday_source().watch_quotes(symbols, max_symbols=max_symbols),
        timeout_seconds=60,
    )


async def intraday_longhu_minutes(symbol: str) -> list[dict[str, Any]]:
    if not longhu_vendor_configured():
        raise RuntimeError("longhu_not_configured")
    return await run_akshare_blocking(
        lambda: longhu_intraday_source().stock_minutes(symbol), timeout_seconds=7,
    )


async def shared_stock_api_call(request: dict[str, Any]) -> dict[str, Any]:
    """Execute or forward one unrestricted documented stock-data call."""
    if not longhu_vendor_configured():
        raise RuntimeError("longhu_not_configured")
    return await run_akshare_blocking(
        lambda: longhu_intraday_source().raw_call(request), timeout_seconds=600,
    )


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
    priority_symbols: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Compatibility entry point backed by the bounded minute-context service."""
    return await capture_intraday_surge_context(
        watches, mapped_peers=mapped_peers, priority_symbols=priority_symbols, cache=_intraday_tencent_minute_cache,
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
    return await latest_fast_quote_confirmations(
        symbols, quotes, observed_at,
        read_latest=lambda items: read_async_latest_fast_quotes(async_db, items),
        confirm=intraday_fast_quote_confirmation,
    )


def _intraday_scan_persistence_dependencies() -> IntradayScanPersistenceServiceDependencies:
    """Compose the declared atomic signal-evidence graph at the ASGI boundary."""
    return IntradayScanPersistenceServiceDependencies(
            database=db,
            confirmation_window=INTRADAY_CONFIRMATION_WINDOW,
            signal_model_version=INTRADAY_SIGNAL_MODEL_VERSION,
            factor_contract_version=INTRADAY_FACTOR_CONTRACT_VERSION,
            # Guards this write against a superseded ``intraday_monitor``
            # leased-loop holder (see runtime_composition.current_lease_fence).
            # ``current_lease_fence`` returns None when this process never
            # acquired that label's lease (e.g. a manual scan trigger), in
            # which case the check below is a no-op.
            lease_key=lease_key_for_label("intraday_monitor"),
            lease_fence=lambda: current_lease_fence("intraday_monitor"),
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
        )


_intraday_scan_persistence_runtime: IntradayScanPersistenceRuntime | None = None


def _intraday_scan_persistence_runtime_instance() -> IntradayScanPersistenceRuntime:
    """Build after all composition ports are defined, on the first real scan."""
    global _intraday_scan_persistence_runtime
    if _intraday_scan_persistence_runtime is None:
        _intraday_scan_persistence_runtime = IntradayScanPersistenceRuntime(
            _intraday_scan_persistence_dependencies(),
        )
    return _intraday_scan_persistence_runtime


def persist_intraday_scan_signals(scan_id: uuid.UUID, observed_at: datetime, selected_symbols: list[str],
                                  source_status: dict[str, Any], watches: list[dict[str, Any]],
                                  quotes: dict[str, dict[str, Any]], all_a_rows: list[dict[str, Any]],
                                  quote_latency_ms: int, tushare_minutes: dict[str, dict[str, Any]],
                                  surge_features: dict[str, dict[str, Any]],
                                  peer_contexts: dict[str, dict[str, Any]],
                                  fast_confirmations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility callback for the watchlist scan service."""
    return _intraday_scan_persistence_runtime_instance().persist(
        scan_id, observed_at, selected_symbols, source_status, watches, quotes, all_a_rows,
        quote_latency_ms, tushare_minutes, surge_features, peer_contexts, fast_confirmations,
    )


_intraday_rule_input_retention = IntradayRuleInputRetentionRuntime(
    IntradayRuleInputRetentionDependencies(
        database=db,
        run_database=run_database_blocking,
        rule_input_retention_days=intraday_rule_input_retention_days,
        ephemeral_signal_retention_days=ephemeral_signal_retention_days,
        prune_rule_inputs=prune_rule_input_evidence,
        prune_ephemeral_events=prune_ephemeral_signal_events,
        prune_change_journal=prune_edge_change_journal,
        change_journal_retention_days=lambda: EDGE_CHANGE_JOURNAL_RETENTION_DAYS,
    ),
)


async def prune_intraday_rule_input_evidence_if_due(observed_at: datetime) -> None:
    """Compatibility entry point for bounded, once-per-date evidence retention."""
    await _intraday_rule_input_retention.prune_if_due(observed_at)


def _intraday_watchlist_scan_runtime() -> IntradayWatchlistScanRuntime:
    """Compose scan I/O ports without putting transactional closures in main."""
    return IntradayWatchlistScanRuntime(IntradayWatchlistScanRuntimeDependencies(
        clock=asyncio.get_running_loop().time,
        observe_duration=lambda status, seconds: intraday_scan_duration_seconds.labels(status).observe(seconds),
        now_utc=lambda: datetime.now(timezone.utc), new_scan_id=uuid.uuid4,
        async_database=async_db, database=db, run_database=run_database_blocking,
        watchlist_capacity=intraday_watchlist_capacity,
        read_watchlists=read_async_intraday_scan_watchlists,
        persist_terminal=persist_intraday_scan_terminal,
        realtime_session=realtime_market_session_async,
        prune_rule_inputs=prune_intraday_rule_input_evidence_if_due,
        retry_pending_alerts=retry_pending_intraday_alerts,
        read_exact_memberships=read_async_exact_watchlist_memberships,
        mapped_peers=pure_mapped_watchlist_peers,
        high_frequency_window=intraday_high_frequency_window,
        quote_capture_dependencies=WatchQuoteCaptureDependencies(
            now=asyncio.get_running_loop().time, all_a_snapshot=intraday_all_a_snapshot,
            tencent_watch_quotes=tencent_order_book_quotes, sina_quotes=sina_quotes,
            eastmoney_watch_flows=eastmoney_watch_flow_quotes,
            watch_flow_reference=intraday_watch_flow_reference,
            watch_volume_fallback=intraday_watch_volume_fallback,
            derive_flow_metrics=derive_intraday_watch_flow_metrics,
            apply_derived_flow_metrics=apply_intraday_derived_watch_flow_metrics,
            derived_flow_divergence=intraday_derived_flow_divergence,
            quote_from_all_a=intraday_quote_from_fuyao,
            merge_eastmoney_flows=merge_intraday_eastmoney_watch_flows,
            annotate_percentiles=annotate_intraday_flow_percentiles,
            annotate_flow_provenance=pure_annotate_flow_snapshot_provenance,
            merge_watch_prices=merge_intraday_watch_quote_prices,
            merge_sina_prices=merge_intraday_sina_watch_quotes,
            quote_freshness=intraday_quote_exchange_time_status,
            consume_background_exception=consume_background_task_exception, safe_error=safe_error_detail,
            executor_saturated_error=ExecutorSaturatedError,
            watch_quote_errors=(httpx.HTTPError, FreeProviderError, ValueError),
            watch_flow_reference_errors=(psycopg.Error, ExecutorSaturatedError, ValueError),
            all_a_snapshot_errors=(FuyaoProviderError, ValueError),
            licensed_watch_quotes=intraday_longhu_watch_quotes,
            merge_licensed_prices=merge_intraday_longhu_watch_quotes,
            licensed_quote_errors=(Exception,),
        ),
        surge_context=intraday_surge_context, peer_context=intraday_peer_context,
        watch_priority_key=intraday_watch_priority_key,
        realtime_validation_slice=intraday_realtime_validation_slice,
        tushare_minutes=intraday_tushare_minutes,
        fast_confirmations=latest_intraday_fast_quote_confirmations,
        board_cache_evidence=intraday_board_cache_evidence,
        build_source_status=build_scan_source_status,
        persist_signals=persist_intraday_scan_signals,
        read_shadow_pool=read_async_ten_day_leader_rotation_pool,
        shadow_rotation_due=ten_day_leader_rotation_intraday_due,
        shadow_rotation_slice=select_intraday_rotation_slice,
        tencent_watch_quotes=tencent_order_book_quotes,
        merge_watch_prices=merge_intraday_watch_quote_prices,
        safe_error=safe_error_detail,
        shadow_quote_errors=(httpx.HTTPError, FreeProviderError, ValueError),
        rotation_persistence_dependencies=TenDayLeaderRotationIntradayDependencies(
            database=db, quote_from_all_a=intraday_quote_from_fuyao,
            quote_source=intraday_quote_observation_source,
            market_context_batch=intraday_point_in_time_market_context_batch,
            evaluate=evaluate_intraday_rotation_candidates,
            persist=persist_intraday_rotation_observations, json_safe=strategy_json_safe,
        ),
        xiaojie_leader_flow=run_xiaojie_leader_flow,
        persist_rotation_observations=persist_ten_day_leader_rotation_intraday,
        persist_rotation_scan_status=persist_intraday_rotation_scan_status,
        json_safe=strategy_json_safe,
        deliver_alert=deliver_intraday_alert, alert_text=intraday_alert_text,
        decision_card_url=decision_card_url, run_scan=run_watchlist_scan,
    ))


async def run_intraday_watchlist_scan(request: IntradayScanRequest) -> dict[str, Any]:
    """Persist a bounded live scan.  The endpoint does not submit orders."""
    return await _intraday_watchlist_scan_runtime().run(request)


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
    suppressed = await suppress_async_legacy_board_rotation_deliveries(async_db)
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
    return Settings.from_environ().strategy_review_automation_enabled


def post_close_strategy_automation_enabled() -> bool:
    return Settings.from_environ().post_close_strategy_automation_enabled


def ten_day_leader_rotation_automation_enabled() -> bool:
    return Settings.from_environ().ten_day_leader_rotation_automation_enabled


def daily_summary_automation_enabled() -> bool:
    return Settings.from_environ().daily_summary_automation_enabled



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


async def ten_day_leader_rotation_loop() -> None:
    """Run the feature's own leased, same-date post-close loop."""
    await run_ten_day_leader_rotation_runtime_loop(TenDayLeaderRotationRuntimeDependencies(
        database=db,
        run_database=run_database_blocking,
        calendar_open=sse_calendar_open_async,
        persisted_completed_for_date=ten_day_leader_rotation_completed_for_date,
        run_materialization=run_ten_day_leader_rotation,
        request=TenDayLeaderRotationRunRequest,
        model_version=TEN_DAY_LEADER_ROTATION_MODEL_VERSION,
        ready_window=ten_day_leader_rotation_ready_window,
        now=lambda: datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")),
        scheduler=ten_day_leader_rotation_scheduler,
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
        dashboard_url=lambda: Settings.from_environ().dashboard_public_url,
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
        freshness_budget_seconds=lambda: runtime_task_contract("super_get_fast_quote").freshness_budget_seconds,
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


async def market_event_capture_loop() -> None:
    """Persist Fuyao all-A auction/pool/chain evidence on a 60s cadence."""
    async def fetch(capability: str, params: dict[str, Any]) -> Mapping[str, Any]:
        from .fuyao_provider import fetch as fetch_fuyao
        return await fetch_fuyao(capability, params)

    async def persist(provider: str, rows: list[dict[str, Any]]) -> int:
        return await run_database_blocking(persist_market_events, provider, rows, timeout_seconds=60)

    async def open_session(now: datetime) -> bool:
        active, _reason = await realtime_market_session_async(now=now)
        return active

    async def all_symbols() -> Sequence[str]:
        return await run_database_blocking(lambda: _market_snapshot_actions.universe_symbols("all_a"), timeout_seconds=15)

    await run_market_event_capture_loop(
        interval_seconds=60, capture=lambda observed_at, **kwargs: capture_market_events(
            observed_at, fetch=fetch, persist=persist, **kwargs,
        ), session_open=open_session, symbols=all_symbols,
    )


async def all_a_level1_snapshot_capture_loop() -> None:
    """Persist one complete all-A Level-1 cross-section about every minute."""
    async def persist(provider: str, capability: str, rows: list[dict[str, Any]]) -> int:
        return await run_database_blocking(
            persist_public_observations, provider, capability, rows, timeout_seconds=90,
        )

    async def capture() -> dict[str, Any]:
        return await capture_level1_snapshot(
            fetch_snapshot=fuyao_all_a_snapshot_rows,
            persist=persist,
            session_open=lambda now: realtime_market_session_async(now=now),
        )

    await run_level1_snapshot_loop(interval_seconds=60, capture=capture)


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


def _persist_local_limit_pool_failure(error: str) -> None:
    """A fuyao outage is fuyao_derived's failure, not AKShare's."""
    with db.transaction() as connection:
        record_provider_failure(connection, "fuyao_derived", "limit_pool", error, None)


def _persist_local_limit_pool(rows: list[dict[str, Any]]) -> int:
    """Store locally derived anchors and their provider health in a DB worker."""
    stored = persist_market_events("fuyao_derived", rows)
    with db.transaction() as connection:
        record_provider_success(connection, "fuyao_derived", "limit_pool", stored, None)
    return stored


async def refresh_intraday_limit_up_anchors(observed_at: datetime) -> dict[str, Any]:
    """Refresh one factual live limit-up pool before linkage mining.

    Derived locally from the licensed all-A snapshot and the session's limit
    prices - the same sealed-ness computation the leader pool runs every scan,
    which covered 100% of the day's true sealed boards on 2026-08-27.  The
    Eastmoney HTML pool this replaces came through AKShare's lxml parser,
    which segfaulted the edge collector at session boundaries five times in a
    week; a locally computed fact has no parser, no vendor availability window
    and no abandoned worker thread.
    """
    trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        reference = await _xiaojie_session_context(trade_date)
        limits = reference.get("limits") or {}
        if not limits:
            return {"status": "blocked", "reason": "session trade limits unavailable"}
        snapshot_rows, _meta = await fuyao_all_a_snapshot_rows()
        rows = live_limit_up_pool_rows(snapshot_rows, limits, reference.get("names"), observed_at)
        stored = await run_database_blocking(_persist_local_limit_pool, rows, timeout_seconds=30)
        return {"status": "completed" if rows else "empty", "received": len(rows), "stored": stored,
                "source": "fuyao_all_a_plus_stk_limit"}
    except ExecutorSaturatedError as error:
        return {"status": "blocked", "reason": safe_error_detail(str(error), 300)}
    except (asyncio.TimeoutError, FuyaoProviderError, ValueError) as error:
        await run_database_blocking(
            _persist_local_limit_pool_failure, str(error) or "limit-up pool request failed")
        return {"status": "unavailable", "reason": safe_error_detail(str(error), 300)}


async def run_limit_linkage_mining(observed_at: datetime, quote_by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Mine non-limit peers without another all-market quote request."""
    async def load_relations(trade_date: date) -> list[dict[str, Any]]:
        return await read_async_limit_linkage_relations(async_db, trade_date)

    async def persist(observed: datetime, trade_date: date, candidates: list[dict[str, Any]], summary: dict[str, Any]) -> str:
        def write() -> str:
            with db.transaction() as connection:
                return persist_limit_linkage_mining_run(
                    connection, observed_at=observed, trade_date=trade_date, candidates=candidates, summary=summary,
                )
        return await run_database_blocking(write)

    return await run_limit_linkage_mining_isolated(
        observed_at, quote_by_symbol,
        LimitLinkageMiningDependencies(
            trade_date=lambda value: value.astimezone(ZoneInfo("Asia/Shanghai")).date(),
            load_relations=load_relations, select_candidates=limit_linkage_candidates,
            persist=persist, safe_error=safe_error_detail,
        ),
    )


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
    """Persist close-daily index context with a labelled public fallback."""
    return await sync_index_context_isolated(
        as_of_date, STRATEGY_INDEX_SYMBOLS,
        prefer_public=longhu_vendor_configured(),
        primary_request=lambda symbol, start, end: TushareFetchRequest(
            api_name="index_daily", provider="primary",
            params={"ts_code": symbol, "start_date": start.strftime("%Y%m%d"),
                    "end_date": end.strftime("%Y%m%d")},
            max_rows=60, force_refresh=True,
        ),
        fetch_primary=fetch_tushare_catalog,
        fetch_public=eastmoney_daily,
        persist_public=persist_free_daily,
        run_database=run_database_blocking,
        fetch_secondary=tencent_index_daily,
    )


async def intraday_surge_context(
    watches: list[dict[str, Any]], *, mapped_peers: dict[str, dict[str, Any]] | None = None,
    priority_symbols: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Use licensed minute paths first and retain Tencent as an automatic fallback."""
    licensed_features: dict[str, dict[str, Any]] = {}
    licensed_status: dict[str, Any] = {
        "provider_status": "disabled", "provider": "longhuvip", "reason": "longhu_not_configured",
    }
    if longhu_vendor_configured():
        licensed_features, licensed_status = await capture_intraday_surge_context(
            watches, mapped_peers=mapped_peers, priority_symbols=priority_symbols,
            cache=_intraday_longhu_minute_cache, max_symbols=intraday_minute_profile_max_symbols,
            open_capabilities=open_provider_capabilities, capability="intraday_minute",
            fetch_minutes=intraday_longhu_minutes, minute_features=intraday_minute_features,
            persist_health=lambda *_args: None, run_database=run_database_blocking,
            safe_error=safe_error_detail, handled_errors=(Exception,),
            provider_key="longhuvip", feature_source="longhuvip_minute",
            check_provider_circuit=False,
        )
    fallback_features, fallback_status = await intraday_tencent_surge_context(
        watches, mapped_peers=mapped_peers, priority_symbols=priority_symbols,
    )
    return {**fallback_features, **licensed_features}, {
        "provider_status": (
            "completed" if licensed_features else str(fallback_status.get("provider_status") or "failed")
        ),
        "primary": licensed_status,
        "fallback": fallback_status,
        "completed": sorted(set(fallback_features) | set(licensed_features)),
        "licensed_completed": sorted(licensed_features),
        "fallback_completed": sorted(set(fallback_features) - set(licensed_features)),
        "policy": "longhuvip_primary_tencent_fallback",
    }


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

async def sync_ths_industry_moneyflow(request: SectorFlowSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated THS industry flow sync."""
    return await sync_ths_industry_isolated(
        request, trade_date=cn_today, fetch_catalog=fetch_tushare_catalog, fetch_request=TushareFetchRequest,
        load_rows=lambda request_key: run_database_blocking(tushare_rows_for_request, request_key),
        run_database_blocking=run_database_blocking, db=db, upsert_taxonomy=upsert_sector_taxonomy, upsert_sector=upsert_sector,
        decimal_or_none=decimal_or_none, json_value=Json, observed_at=lambda: datetime.now(timezone.utc),
    )


async def sync_ths_concept_signals(request: SectorFlowSyncRequest) -> dict[str, Any]:
    """Compatibility entry point backed by isolated THS concept flow sync."""
    return await sync_ths_concept_signals_isolated(
        request, trade_date=cn_today, fetch_catalog=fetch_tushare_catalog, fetch_request=TushareFetchRequest,
        load_rows=lambda request_key: run_database_blocking(tushare_rows_for_request, request_key),
        run_database_blocking=run_database_blocking, db=db, upsert_taxonomy=upsert_sector_taxonomy, upsert_sector=upsert_sector,
        decimal_or_none=decimal_or_none, json_value=Json, observed_at=lambda: datetime.now(timezone.utc), http_exception=HTTPException,
    )


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
    return Settings.from_environ().ths_concept_member_backfill_enabled


def ths_concept_member_backfill_batch_size() -> int:
    return Settings.from_environ().ths_concept_member_backfill_batch_size


def all_board_member_backfill_enabled() -> bool:
    return Settings.from_environ().all_board_member_backfill_enabled


def all_board_member_backfill_batch_size() -> int:
    return Settings.from_environ().all_board_member_backfill_batch_size


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
    """Compose the durable all-board backfill batch with its polling cadence."""
    async def run_batch() -> None:
        await run_all_board_member_backfill_batch(AllBoardMemberBackfillRequest(
            batch_size=all_board_member_backfill_batch_size(), include_ths=True, include_eastmoney=True,
        ))

    def log_failure(message: str) -> None:
        logger.warning(
            f"all board member backfill batch failed: {safe_error_detail(message, 300)}",
            extra={"task": "all_board_member_backfill"},
        )

    await run_all_board_member_backfill_loop_runtime(AllBoardMemberBackfillLoopDependencies(
        sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch, log_failure=log_failure,
    ))


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
    """Compose the fail-closed THS concept member batch with its polling cadence."""
    async def run_batch() -> None:
        await run_ths_concept_member_backfill_batch(ConceptMemberBackfillRequest(batch_size=ths_concept_member_backfill_batch_size()))

    def log_failure(message: str) -> None:
        logger.warning(
            f"THS concept member backfill batch failed: {safe_error_detail(message, 300)}",
            extra={"task": "ths_member_backfill"},
        )

    await run_ths_concept_member_backfill_loop_runtime(ThsConceptMemberBackfillLoopDependencies(
        sse_calendar_open_async=sse_calendar_open_async, run_batch=run_batch, log_failure=log_failure,
    ))


#: Audit B-HIGH / WP7 follow-up: quant.retention_policies rows all start
#: enabled=false (see migration 20260902_0085), so this task does nothing on
#: a fresh deploy even if this automation flag is on. Both gates must be
#: explicitly opted into before any row is ever deleted.
RETENTION_MAINTENANCE_TASK_KEY = "retention_maintenance"
RETENTION_MAINTENANCE_MAX_BATCHES_PER_TABLE = 50


def retention_maintenance_automation_enabled() -> bool:
    """Off by default: this task is new, deletes data, and has no operator history yet."""
    return Settings.from_environ().retention_maintenance_automation_enabled


def _enabled_retention_policy_tables(database: Any) -> list[str]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT table_name FROM quant.retention_policies WHERE enabled ORDER BY table_name",
        ).fetchall()
    return [str(row["table_name"]) for row in rows]


def _apply_one_retention_batch(database: Any, table_name: str) -> int:
    # Each batch is its own transaction (matches retention_maintenance.py's
    # existing manual operator script): a large delete must not hold locks
    # or an open transaction for the whole multi-table run.
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT deleted_rows FROM quant.apply_retention_policy(%s)", (table_name,),
        ).fetchone()
    return int(row["deleted_rows"] or 0) if row else 0


def _run_retention_maintenance_operation() -> dict[str, Any]:
    tables = _enabled_retention_policy_tables(db)
    results: dict[str, Any] = {}
    for table_name in tables:
        total_deleted, batches = 0, 0
        try:
            for _ in range(RETENTION_MAINTENANCE_MAX_BATCHES_PER_TABLE):
                deleted = _apply_one_retention_batch(db, table_name)
                total_deleted += deleted
                batches += 1
                if deleted == 0:
                    break
        except Exception as error:  # noqa: BLE001 - one table's failure must not block the others
            results[table_name] = {"status": "failed", "error": safe_error_detail(str(error), 300)}
            continue
        results[table_name] = {"status": "completed", "deleted_rows": total_deleted, "batches": batches}
    return {"status": "completed", "tables": results, "enabled_tables": len(tables)}


async def run_retention_maintenance() -> dict[str, Any]:
    """Run one durable, deduplicated retention pass (see run_recorded)."""
    today = cn_today()
    return await run_database_blocking(functools.partial(
        run_recorded, db, task_key=RETENTION_MAINTENANCE_TASK_KEY, run_key=f"retention-maintenance:{today}",
        operation=_run_retention_maintenance_operation, cadence="daily", as_of_date=today,
        methodology_version="retention-maintenance-v1",
    ), timeout_seconds=280, lane="batch")


async def retention_maintenance_loop() -> None:
    """Once daily, after the post-close window, apply every enabled retention policy."""
    completed_dates: set[date] = set()
    while True:
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        exchange_date = local.date()
        if exchange_date not in completed_dates and time(16, 30) <= local.time() < time(18, 30):
            try:
                result = await run_retention_maintenance()
                if str(result.get("status") or "") in {
                    "completed", "partial", "skipped_running_elsewhere", "skipped_in_flight_process",
                }:
                    completed_dates.add(exchange_date)
            except Exception as error:  # noqa: BLE001 - retry within the same window on the next tick
                logger.warning(
                    f"retention maintenance failed: {safe_error_detail(str(error), 300)}",
                    extra={"task": "retention_maintenance"},
                )
            await asyncio.sleep(120)
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


def market_snapshot_fuyao_enabled() -> bool:
    return _market_snapshot_actions.fuyao_enabled()


def fuyao_snapshot_quotes(rows: list[dict[str, Any]], exchange_date: date) -> list[dict[str, Any]]:
    return _market_snapshot_actions.fuyao_quotes(rows, exchange_date, intraday_quote_from_fuyao)


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
    fuyao_status: dict[str, Any],
) -> dict[str, Any]:
    return _market_snapshot_actions.finalize(
        request, observed_at, exchange_date, symbols, minimum_universe, minimum_coverage,
        licensed_providers, public_quote_settings, planned_public_requests, refresh_error,
        refresh_skipped, fuyao_status,
    )


async def build_market_snapshot(request: MarketSnapshotRequest) -> dict[str, Any]:
    """Build a bounded snapshot through the isolated provider/persistence service."""
    return await _market_snapshot_actions.build(
        request,
        run_database=run_database_blocking,
        fetch_fuyao_all_a=fuyao_all_a_snapshot_rows,
        provider_capabilities=open_provider_capabilities,
        quote_mapper=intraday_quote_from_fuyao,
        thresholds=market_snapshot_thresholds,
        public_quote_settings=market_snapshot_public_quote_settings,
        fuyao_enabled=market_snapshot_fuyao_enabled,
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


async def _post_close_core_symbols(limit: int) -> list[str]:
    return await read_async_limited_core_symbols(async_db, limit)


async def check_post_close_refresh_lease_fence(database: Any, lease_key: str, fence: int) -> None:
    """Run the fencing-token check through the bounded database executor."""
    await run_database_blocking(check_runtime_lease_fence, database, lease_key, fence, timeout_seconds=10)


def _post_close_refresh_dependencies() -> PostCloseRefreshDependencies:
    """Compose the local-only boundaries of the post-close application service."""
    return PostCloseRefreshDependencies(
        database=db, china_today=cn_today, longhu_configured=longhu_vendor_configured,
        longhu_close_context=lambda trade_date: read_longhu_close_context(db, trade_date),
        provider_configs=provider_configs, run_database=run_database_blocking,
        reconcile_stale_fetch_runs=reconcile_stale_fetch_runs, reprocess_remote_reports=reprocess_remote_reports,
        reconcile_stale_automation_runs=reconcile_stale_automation_runs,
        sync_market_universe=sync_market_universe, sync_full_market_daily=sync_full_market_daily,
        sync_strategy_index_context=sync_strategy_index_context, build_market_snapshot=build_market_snapshot,
        load_core_symbols=_post_close_core_symbols, akshare_probe=akshare_probe,
        sync_ths_industry_flow=sync_ths_industry_moneyflow, sync_ths_concept_flow=sync_ths_concept_signals,
        rebuild_market_flow_features=rebuild_stored_market_flow_features,
        refresh_pattern_sources=refresh_strategy_pattern_sources, run_pattern_mining=run_strategy_pattern_mining,
        persist_settled_limit_pool=persist_settled_limit_pool,
        sync_daily_controls=sync_full_market_daily_controls, sync_cninfo_announcements=sync_cninfo_announcements,
        run_board_report=run_intraday_board_report, run_strategy_decision=run_strategy_decision,
        persist_close_review=_persist_close_review, recompute_outcomes=recompute_outcomes,
        recompute_intraday_outcomes=recompute_analyst_intraday_outcomes_for_date,
        recompute_scorecards=recompute_scorecards, rebuild_analyst_research=rebuild_analyst_research_for_date,
        run_post_close_strategy=run_post_close_strategy, persist_watchlist_main_wave=persist_watchlist_main_wave_research,
        refresh_decision_research=refresh_decision_research_and_plans,
        build_research_snapshot=build_snapshot, run_orchestrator=run_post_close_refresh_orchestrated,
        record_stage=record_stage_with_receipt, lease_key=POST_CLOSE_REFRESH_LEASE_KEY,
        lease_seconds=post_close_refresh_lease_seconds, acquire_lease=acquire_runtime_lease,
        renew_lease=renew_runtime_lease, release_lease=release_runtime_lease,
        safe_error_detail=safe_error_detail, json_safe=strategy_json_safe,
        check_lease_fence=check_post_close_refresh_lease_fence,
        post_close_strategy_model_version=POST_CLOSE_STRATEGY_MODEL_VERSION,
    )


async def run_post_close_refresh(request: PostCloseRefreshRequest) -> dict[str, Any]:
    """Compatibility entry point for the isolated same-date refresh assembly."""
    return await run_post_close_refresh_isolated(request, _post_close_refresh_dependencies())


def _persist_close_review(as_of_date: date) -> dict[str, Any]:
    return persist_strategy_review_runtime(
        db,
        strategy_review_payload,
        StrategyReviewRequest(session="close", as_of_date=as_of_date, persist=True),
    )


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
    """Compatibility adapter for the isolated bounded public-source probe."""
    return await fetch_stock_study_public(
        label, provider, capability, fetcher, symbol,
        StockStudyPublicDependencies(
            open_provider_capabilities=open_provider_capabilities,
            run_database=run_database_blocking,
            persist_success=persist_stock_study_free_result,
            persist_failure=persist_stock_study_free_failure,
            safe_error_detail=safe_error_detail,
            request_errors=(httpx.HTTPError, FreeProviderError, AkShareProviderError, ValueError),
        ),
    )


def study_number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def study_date_key(row: dict[str, Any]) -> str:
    return str(row.get("trade_date") or row.get("date") or "")


def latest_study_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(rows, key=study_date_key) if rows else None


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
    """Compatibility adapter for explicit-symbol, same-day controls only."""
    return await sync_core_daily_controls_isolated(
        as_of_date, requested_symbols,
        CoreDailyControlDependencies(
            resolve_symbols=resolve_sync_symbols_async,
            fetch_catalog=fetch_tushare_catalog,
            request=TushareFetchRequest,
        ),
    )


def _start_application_background_tasks() -> dict[str, asyncio.Task[None]]:
    """Create the uniquely-labelled leased runtime loops after local startup."""
    if not background_tasks_enabled():
        return {}
    interval_seconds = intraday_scan_interval_seconds()
    lease_holder_id = uuid.uuid4()
    lease_seconds = background_loop_lease_seconds()
    leased_background_loop = build_leased_task_runner(LeasedRuntimeDependencies(
        database=async_db,
        lease_holder_id=lease_holder_id,
        lease_seconds=lease_seconds,
        acquire_lease=acquire_background_runtime_lease,
        renew_lease=renew_background_runtime_lease,
        release_lease=release_background_runtime_lease,
        supervise=supervise_leased_loop,
        on_state=background_loop_registry.mark,
    ))

    specs = build_background_task_specs(
        interval_seconds=interval_seconds,
        enabled={
            "intraday_monitor": interval_seconds >= 30,
            "super_get_fast_quote": interval_seconds >= 30,
            "strategy_review": strategy_review_automation_enabled(),
            "post_close_strategy": post_close_strategy_automation_enabled(),
            "ten_day_leader_rotation": ten_day_leader_rotation_automation_enabled(),
            "daily_strategy_summary": daily_summary_automation_enabled(),
            "ths_member_backfill": ths_concept_member_backfill_enabled(),
            "all_board_member_backfill": all_board_member_backfill_enabled(),
            "minute_profile_capture": intraday_minute_profile_capture_enabled(),
            "tencent_order_book": intraday_order_book_enabled() and interval_seconds >= 30,
            "board_flow_curve": intraday_board_curve_enabled(),
            "market_event_capture": Settings.from_environ().market_event_capture_enabled,
            "all_a_level1_snapshot": Settings.from_environ().all_a_level1_capture_enabled,
        },
        loops={
            "intraday_monitor": lambda: intraday_monitor_loop(interval_seconds),
            "super_get_fast_quote": intraday_super_get_fast_quote_loop, "strategy_review": strategy_review_loop,
            "post_close_strategy": post_close_strategy_loop, "ten_day_leader_rotation": ten_day_leader_rotation_loop,
            "daily_strategy_summary": daily_strategy_summary_loop, "ths_member_backfill": ths_concept_member_backfill_loop,
            "all_board_member_backfill": all_board_member_backfill_loop,
            "minute_profile_capture": intraday_minute_profile_capture_loop, "tencent_order_book": intraday_order_book_loop,
            "board_flow_curve": intraday_board_flow_curve_loop,
            "market_event_capture": market_event_capture_loop,
            "all_a_level1_snapshot": all_a_level1_snapshot_capture_loop,
        },
    )
    # ``background_task_catalog.build_specs`` enumerates a fixed label tuple
    # that does not include this task; append it directly here so a new
    # entry does not require touching that shared, hand-enumerated catalog.
    specs = (*specs, BackgroundTaskSpec(
        RETENTION_MAINTENANCE_TASK_KEY, retention_maintenance_automation_enabled(), retention_maintenance_loop,
    ))
    validate_runtime_task_specs(specs)
    return start_leased_background_tasks(
        apply_background_runtime_profile(specs),
        leased_background_loop,
    )


def _verify_strategy_runtime_contracts() -> None:
    """Verify runtime model identities before any strategy loop obtains a lease."""
    validate_strategy_runtime_versions({
        "intraday_watchlist_confirmation": INTRADAY_SIGNAL_MODEL_VERSION,
        "watchlist_main_wave_shadow": WATCHLIST_MAIN_WAVE_MODEL_VERSION,
        "countertrend_rebound_shadow": WATCHLIST_REBOUND_MODEL_VERSION,
        "ten_day_leader_rotation_shadow": TEN_DAY_LEADER_ROTATION_MODEL_VERSION,
        "post_close_base_candidates": POST_CLOSE_STRATEGY_MODEL_VERSION,
        "post_close_limit_lift_pattern": STRATEGY_PATTERN_MODEL_VERSION,
        "disclosure_day_watch": DISCLOSURE_DAY_WATCH_MODEL_VERSION,
        "limit_up_continuation": LIMIT_UP_CONTINUATION_MODEL_VERSION,
        "xiaojie_leader_flow": XIAOJIE_LEADER_FLOW_MODEL_VERSION,
    })


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
        verify_strategy_contracts=_verify_strategy_runtime_contracts,
        resolve_write_api_key=resolve_write_api_key,
        control_plane_writes_enabled=control_plane_writes_enabled,
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


def _set_db_pool_gauge(pool: dict[str, Any]) -> None:
    db_pool_connections.labels("size").set(pool["pool_size"])
    db_pool_connections.labels("available").set(pool["available"])
    db_pool_connections.labels("waiting").set(pool["waiting"])


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
            _set_db_pool_gauge(db.pool_status())
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
app.include_router(build_longhu_reads_router(
    configured=longhu_vendor_configured,
    shared_read_key=lambda: Settings.from_environ().shared_read_api_key,
    quotes=shared_longhu_quotes,
    minutes=intraday_longhu_minutes,
))
app.include_router(build_licensed_stock_api_router(
    configured=longhu_vendor_configured,
    shared_read_key=lambda: Settings.from_environ().shared_read_api_key,
    call=shared_stock_api_call,
))
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


async def record_l2_research_evaluation(payload: L2IncrementalEvaluationRequest) -> dict[str, Any]:
    """Evaluate and persist licensed L2 evidence on the database executor."""
    evaluation = evaluate_l2_incremental_value(
        [row.model_dump() for row in payload.rows], minimum_samples=payload.minimum_samples,
    )
    return await run_database_blocking(
        persist_l2_evaluation, db,
        source_kind=payload.source_kind,
        algorithm_version=payload.algorithm_version,
        minimum_samples=payload.minimum_samples,
        evaluation=evaluation,
        evidence_window_start=payload.evidence_window_start,
        evidence_window_end=payload.evidence_window_end,
        timeout_seconds=30,
    )


async def latest_l2_research_evaluation() -> dict[str, Any]:
    return await latest_l2_evaluation(async_db)


app.include_router(build_l2_research_router(L2ResearchDependencies(
    record=record_l2_research_evaluation,
    latest=latest_l2_research_evaluation,
)))


app.include_router(build_strategy_reads_router(db, STRATEGY_DECISION_MODEL_VERSION, async_db, cn_today=cn_today))
app.include_router(build_paper_reads_router(db, async_db))
app.include_router(build_paper_actions_router(db, configure_paper_account, accept_paper_decision))
app.include_router(build_personal_decisions_router(PersonalDecisionDependencies(
    database=db,
    async_database=async_db,
    persist_snapshot=persist_broker_snapshot,
    persist_plan=persist_trade_plan,
    latest_snapshot=latest_broker_snapshot,
    latest_brief=latest_personal_decision_brief,
    latest_research=latest_decision_research,
)))
app.include_router(build_analyst_prompt_lab_router(
    db, materialize_prompt_candidates, label_prompt_candidate, evaluate_prompt_variant,
    materialize_intraday_analyst_outcomes, async_database=async_db,
))
app.include_router(build_strategy_pattern_reads_router(
    db, merge_limit_pool_sources, limit_board_count, strategy_json_safe,
    post_close_limit_daily_features, post_close_exact_board_context, post_close_tushare_lhb_context, async_db,
    run_database_blocking,
))
app.include_router(build_ten_day_leader_rotation_reads_router(async_db))
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


# ``QUANT_WRITE_API_KEY`` used to be read fresh from the environment on every
# request, which silently allowed every write when it was unset (fail-open)
# and gave the middleware no natural place to fail closed at startup.  It is
# now resolved once by ``resolve_write_api_key`` (invoked from the real
# application lifespan; see ``_application_lifecycle_dependencies``) and
# cached here.  Tests that bypass the real lifespan configure this directly.
_write_api_key_state: dict[str, str] = {"value": ""}


def configured_write_api_key() -> str:
    """Return the write API key resolved once at startup."""
    return _write_api_key_state["value"]


def resolve_write_api_key() -> None:
    """Fail closed at startup when no write key is configured.

    ``QUANT_ALLOW_UNAUTHENTICATED_WRITES=1`` is an explicit, logged opt-out
    for local development; production deployments must set
    ``QUANT_WRITE_API_KEY`` or the process refuses to start.
    """
    settings = Settings.from_environ()
    key = settings.write_api_key
    allow_unauthenticated = settings.allow_unauthenticated_writes
    if not key and not allow_unauthenticated:
        raise RuntimeError("QUANT_WRITE_API_KEY is required")
    if not key:
        logger.warning(
            "QUANT_WRITE_API_KEY is not set; QUANT_ALLOW_UNAUTHENTICATED_WRITES=1 "
            "permits every write request without a key. Do not use this outside local development."
        )
    _write_api_key_state["value"] = key


@app.middleware("http")
async def require_quant_write_key(request: Request, call_next: Any) -> Any:
    configured_key = configured_write_api_key()
    supplied_key = request.headers.get("X-Quant-Write-Key")
    licensed_read = licensed_stock_read_allowed(
        request, Settings.from_environ().shared_read_api_key,
    )
    if not write_access_allowed(request.method, supplied_key, configured_key) and not licensed_read:
        return JSONResponse(status_code=401, content={"detail": "valid X-Quant-Write-Key is required for write operations"})
    # The remote-archive-sync trigger is not a bypass of the write key above:
    # it always needs the write key like any other write route, and on top
    # of that must present a bearer-shaped Authorization header before this
    # service will contact the remote archive and record an automation run.
    if (
        request.method.upper() == "POST"
        and request.url.path == "/api/v1/remote-archive/sync"
        and not remote_archive_sync_bearer_allowed(request)
    ):
        return JSONResponse(status_code=401, content={"detail": "remote archive sync requires a bearer-shaped Authorization header"})
    return await call_next(request)


def _health_payload() -> dict[str, Any]:
    """Build local runtime evidence without touching market providers."""
    return read_health_payload(HealthDependencies(
            database=db, post_close_lease_key=POST_CLOSE_REFRESH_LEASE_KEY,
            background_loop_lease_seconds=background_loop_lease_seconds,
            data_directory=lambda: Path(Settings.from_environ().data_dir),
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
            set_db_pool_gauge=_set_db_pool_gauge, set_open_circuit_gauge=provider_circuit_open.set,
            research_storage_governance=local_research_storage_governance,
            background_loop_status=background_loop_registry.snapshot,
            runtime_task_contracts=runtime_task_contract_catalog,
            optional_background_tasks=lambda: {
                "background_tasks_enabled": background_tasks_enabled(),
                "runtime_profile": background_runtime_profile(),
                "background_loop:ths_member_backfill": ths_concept_member_backfill_enabled(),
                "background_loop:all_board_member_backfill": all_board_member_backfill_enabled(),
            },
            daily_control_plane_status=full_market_daily_control_status,
            live_session_acceptance_status=read_live_session_acceptance,
            release_metadata=release_metadata,
            post_close_runtime_status=post_close_refresh_runtime.status,
            control_plane_writes_enabled=control_plane_writes_enabled,
    ))


def _metrics_response() -> Response:
    """Local Prometheus scrape response; service remains loopback-bound."""
    refresh_metrics_control_plane()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)




def intraday_services_status_payload() -> dict[str, Any]:
    """Build the status board through the independent local read model."""
    return read_intraday_services_status_payload(_intraday_status_dependencies())


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


app.include_router(build_system_control_router(SystemControlDependencies(
    health_payload=_health_payload,
    database_unavailable_error=DatabaseUnavailableError,
    metrics_response=_metrics_response,
)))


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


async def fuyao_query(payload: "FuyaoQueryRequest") -> dict[str, Any]:
    """Expose every documented Fuyao capability through the safe allow-list."""
    from .fuyao_provider import fetch_envelope as fetch_fuyao
    envelope = await fetch_fuyao(payload.capability, payload.params)
    return {
        "provider": "fuyao_ths", "capability": payload.capability,
        "request_id": envelope["request_id"], "message": envelope["message"],
        "data": envelope["data"], "research_only": True, "live_effect": "none",
    }


async def stock_study(symbol: str, payload: StockStudyRequest | None = None) -> dict[str, Any]:
    """Compatibility service function used by the provider-actions router."""
    return await build_stock_study(symbol, payload or StockStudyRequest())


app.include_router(build_provider_actions_router(ProviderActionDependencies(
    akshare_probe=akshare_probe,
    realtime_probe=probe_realtime_sources,
    tushare_audit=audit_tushare_capabilities,
    tushare_fetch=tushare_fetch,
    fuyao_query=fuyao_query,
    stock_study=stock_study,
)))



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


def review_claim(review_id: uuid.UUID, payload: ClaimReviewRequest) -> dict[str, Any]:
    """Compatibility entry point for point-in-time safe claim review."""
    return review_claim_isolated(review_id, payload, database=db, exchange_for=exchange_for)



def update_universe_members(payload: UniverseUpdateRequest) -> dict[str, Any]:
    return update_universe_members_isolated(payload, _research_maintenance_dependencies())


def build_features(payload: GenerateRequest) -> dict[str, Any]:
    return build_feature_snapshot(payload.as_of_date or cn_today(), payload.universe_key)



def _research_experiment_dependencies() -> ResearchExperimentDependencies:
    return ResearchExperimentDependencies(
        database=db, china_today=cn_today, as_utc=as_utc, http_exception=HTTPException,
        evaluate_factor_set=evaluate_factor_set, run_multi_factor_strategy=run_multi_factor_strategy_sql,
        json_value=Json,
    )




def evaluate_factors(payload: FactorEvaluationRequest) -> dict[str, Any]:
    return evaluate_factors_isolated(payload, _research_experiment_dependencies())




def backtest_strategy(payload: StrategyBacktestRequest) -> dict[str, Any]:
    return backtest_strategy_isolated(payload, _research_experiment_dependencies())



def reconcile_stale_fetch_runs(payload: FetchRunReconcileRequest) -> dict[str, Any]:
    return reconcile_stale_fetch_runs_isolated(payload, _research_maintenance_dependencies())


def reconcile_stale_automation_runs() -> dict[str, Any]:
    return reconcile_stale_automation_runs_isolated(_research_maintenance_dependencies())



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


def _intraday_replay_dependencies() -> IntradayReplayDependencies:
    return IntradayReplayDependencies(
        database=db, model_version=INTRADAY_SIGNAL_MODEL_VERSION, signal_rules=intraday_signal_rules,
    )


def replay_recorded_intraday_rule_inputs(payload: IntradayRuleInputReplayRequest) -> dict[str, Any]:
    """Compatibility export backed by the intraday replay service module."""
    return intraday_replay_service.replay_recorded_intraday_rule_inputs(payload, _intraday_replay_dependencies())


async def replay_recorded_intraday_rule_inputs_endpoint(payload: IntradayRuleInputReplayRequest) -> dict[str, Any]:
    return await run_database_blocking(replay_recorded_intraday_rule_inputs, payload, timeout_seconds=60)


def _intraday_entry_timing_challenger_dependencies() -> IntradayEntryTimingChallengerDependencies:
    return IntradayEntryTimingChallengerDependencies(
        database=db, model_version=INTRADAY_SIGNAL_MODEL_VERSION, pure_signal_rules=pure_intraday_signal_rules,
        number=intraday_number, upside_assessment=intraday_upside_research_assessment,
    )


def run_intraday_entry_timing_challengers(payload: IntradayRuleInputReplayRequest) -> dict[str, Any]:
    """Compatibility export backed by the intraday replay service module."""
    return intraday_replay_service.run_intraday_entry_timing_challengers(
        payload, _intraday_entry_timing_challenger_dependencies(),
    )


async def run_intraday_entry_timing_challengers_endpoint(payload: IntradayRuleInputReplayRequest) -> dict[str, Any]:
    return await run_database_blocking(run_intraday_entry_timing_challengers, payload, timeout_seconds=120)


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
    run_entry_timing_challengers=run_intraday_entry_timing_challengers_endpoint,
)))



def import_bars(payload: BarsImport) -> dict[str, int]:
    with db.transaction() as connection:
        for bar in payload.bars:
            upsert_bar(connection, bar)
    return {"imported": len(payload.bars)}


async def sync_market_universe_endpoint(payload: MarketUniverseSyncRequest) -> dict[str, Any]:
    return await sync_market_universe(payload)


async def sync_full_market_daily_endpoint(payload: FullMarketDailySyncRequest) -> dict[str, Any]:
    return await sync_full_market_daily(payload)


async def sync_full_market_daily_controls_endpoint(
    payload: FullMarketDailyControlsSyncRequest,
) -> dict[str, Any]:
    return await sync_full_market_daily_controls(payload.trade_date)


def _require_post_close_strategy_ownership() -> None:
    """Refuse a one-click refresh on a process that does not own this task.

    Without this, an ``intraday_edge`` process could trigger the same
    research-side writes as the ``research`` owner through this HTTP endpoint,
    racing the scheduled post-close loop's lease/run_key even though the
    background loop itself is correctly disabled on this profile.
    """
    profile = background_runtime_profile()
    if not runtime_profile_owns_task(profile, "post_close_strategy"):
        raise HTTPException(
            status_code=409,
            detail=f"this instance (runtime profile={profile}) does not own post_close_strategy",
        )


async def post_close_refresh_endpoint(payload: PostCloseRefreshRequest) -> dict[str, Any]:
    _require_post_close_strategy_ownership()
    return await post_close_refresh_runtime.run(lambda: run_post_close_refresh(payload))


async def start_post_close_refresh_endpoint(payload: PostCloseRefreshRequest) -> dict[str, Any]:
    _require_post_close_strategy_ownership()
    return post_close_refresh_runtime.start(lambda: run_post_close_refresh(payload))


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
    sync_full_daily_controls=sync_full_market_daily_controls_endpoint,
    post_close_refresh=post_close_refresh_endpoint,
    start_post_close_refresh=start_post_close_refresh_endpoint,
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
    return await run_database_blocking(
        persist_strategy_review_runtime, db, strategy_review_payload, payload, timeout_seconds=30,
    )


async def run_post_close_strategy_endpoint(payload: PostCloseStrategyRequest) -> dict[str, Any]:
    return await run_database_blocking(run_post_close_strategy, payload, timeout_seconds=60)


async def run_ten_day_leader_rotation_endpoint(
    payload: TenDayLeaderRotationRunRequest,
) -> dict[str, Any]:
    return await run_database_blocking(run_ten_day_leader_rotation, payload, timeout_seconds=90)


async def run_strategy_pattern_mining_endpoint(payload: StrategyPatternMiningRequest) -> dict[str, Any]:
    return await run_strategy_pattern_mining(payload)


def persist_watchlist_main_wave_research(payload: WatchlistMainWaveResearchRequest) -> dict[str, Any]:
    """Fit and persist breakout plus counter-trend watchlist shadow models."""
    return WatchlistShadowResearchRuntime(WatchlistShadowResearchRuntimeDependencies(
        database=db,
        main_wave_research=run_watchlist_main_wave_v2_research,
        rebound_research=run_countertrend_rebound_research,
        main_wave_key=WATCHLIST_MAIN_WAVE_STRATEGY_KEY,
        rebound_key=WATCHLIST_REBOUND_STRATEGY_KEY,
        china_today=cn_today,
        json_safe=strategy_json_safe,
        json_value=Json,
    )).persist(payload)


async def run_watchlist_main_wave_endpoint(payload: WatchlistMainWaveResearchRequest) -> dict[str, Any]:
    return await run_database_blocking(persist_watchlist_main_wave_research, payload, timeout_seconds=90)



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
        rows = await read_async_enabled_intraday_watches(
            async_db, max_symbols=intraday_minute_profile_max_symbols(),
        )
        symbols = [str(row["symbol"]) for row in sorted(rows, key=intraday_watch_priority_key)]
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







async def run_market_snapshot_endpoint(payload: MarketSnapshotRequest) -> dict[str, Any]:
    return await build_market_snapshot(payload)



def import_offline_minute_bars(payload: OfflineMinuteImportRequest) -> dict[str, Any]:
    try:
        return import_offline_minute_csv(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def import_offline_minute_bars_endpoint(payload: OfflineMinuteImportRequest) -> dict[str, Any]:
    return await run_database_blocking(import_offline_minute_bars, payload, timeout_seconds=60)



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



async def outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    return await run_database_blocking(recompute_outcomes, as_of_date, timeout_seconds=60)


async def intraday_outcomes(as_of_date: date | None = None) -> dict[str, Any]:
    return await run_database_blocking(recompute_intraday_signal_outcomes, as_of_date, timeout_seconds=60)



async def recommendations(payload: GenerateRequest) -> dict[str, Any]:
    return await run_database_blocking(generate_recommendations, payload, timeout_seconds=30)


async def run_daily_pipeline(payload: GenerateRequest) -> dict[str, Any]:
    return await run_daily_pipeline_orchestrated(
        payload, sync_full_market_daily=sync_full_market_daily, sync_baostock=sync_baostock,
        sync_full_market_daily_controls=sync_full_market_daily_controls,
        tushare_request=TushareSyncRequest, full_market_request=FullMarketDailySyncRequest,
        snapshot_request=lambda as_of: SnapshotRequest(as_of_date=as_of), build_snapshot=build_snapshot,
        recompute_outcomes=recompute_outcomes, recompute_scorecards=recompute_scorecards,
        generate_recommendations=generate_recommendations, run_database_blocking=run_database_blocking,
        cn_today=cn_today, materialize_regime=materialize_market_regime_today,
        materialize_sentiment_cycle=materialize_sentiment_cycle_today,
        materialize_candidate_ledger=materialize_strategy_daily_candidate_ledger,
        sync_earnings_calendar=sync_earnings_calendar,
        sync_stock_money_flow=sync_stock_money_flow,
        materialize_watchlist_proposals=materialize_daily_watchlist_proposals,
        settle_xiaojie_outcomes=settle_xiaojie_leader_flow_outcomes,
        backfill_minute_bars=backfill_session_minute_bars,
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
app.include_router(build_xiaojie_leader_flow_router(evaluate_xiaojie_leader_flow_snapshot))
app.include_router(build_ten_day_leader_rotation_actions_router(
    TenDayLeaderRotationActionDependencies(run=run_ten_day_leader_rotation_endpoint),
))




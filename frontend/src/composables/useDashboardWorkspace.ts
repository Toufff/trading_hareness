
import { computed, onBeforeUnmount, onMounted, provide, proxyRefs, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { DataAnalysis, Document, Operation, Refresh, UploadFilled, WarningFilled } from '@element-plus/icons-vue';
import VChart from 'vue-echarts';
import { use } from 'echarts/core';
import { BarChart, CandlestickChart, LineChart, ScatterChart } from 'echarts/charts';
import { DataZoomComponent, GridComponent, LegendComponent, MarkPointComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { AnalystMarketReview, AutomationRun } from '../api/analyst-contract';
import { decodeJson, getJson, postJson } from '../api/http';
import { usePolling } from './usePolling';
import { dashboardContextKey } from '../dashboard-context';



export function useDashboardWorkspace() {
use([BarChart, CandlestickChart, LineChart, ScatterChart, DataZoomComponent, GridComponent, LegendComponent, MarkPointComponent, TooltipComponent, CanvasRenderer]);

type Route = { tag: string; label: string };
type EventItem = { event_id: string; received_at: string; message_type?: string; text?: string; source_label?: string; n8n_status?: string; target_status?: string; target_batch_id?: string | null; n8n_error?: string | null };
type ProviderConfig = { name: string; provider_key: string; label: string; configured: boolean; protocol: string; rate_limit_per_minute?: number; min_interval_seconds?: number; realtime_coverage?: string; realtime_note?: string; realtime_apis?: string[]; super_alias_first_apis?: string[]; get_apis?: string[]; complete_query_apis?: string[]; bounded_only_apis?: string[]; reconciliation_required_apis?: string[] };
type Availability = 'declared' | 'verified' | 'empty' | 'unsupported' | 'failed' | 'unknown';
type ProviderObservation = { availability: Availability; verified_at?: string | null; last_checked_at?: string | null; last_row_count?: number | null; last_observation?: string | null };
type CatalogItem = { api_name: string; group: string; normalized: boolean; status?: string; frequency?: string; decision_eligible?: boolean; preferred_providers?: string[]; note?: string; catalog_origin: string; permission_model: string; min_points?: number | null; official_doc_url?: string | null; request_policy: string; model_role: string; priority: string; sample_params?: Record<string, unknown> | null; primary_availability: Availability; super_availability: Availability; super_sdk_availability: Availability; super_get_availability: Availability; provider_observations?: Record<string, ProviderObservation> };
type CatalogCounts = { total?: number; supplier_109?: number; official_extensions?: number; points_at_or_below_15000?: number; market_hours_only?: number; offline_files_only?: number; primary_verified?: number; super_verified?: number; super_sdk_verified?: number; super_get_verified?: number; primary_responded?: number; super_responded?: number; super_sdk_responded?: number; super_get_responded?: number };
type CapabilityAuditRow = { api_name: string; provider: string; status: string; availability: Availability; received?: number; stored?: number; reason?: string; params?: Record<string, unknown> };
type MarketSnapshot = { session: string; exchange_date: string; observed_at: string; universe_key: string; universe_count: number; quote_count: number; coverage: number; status: string; decision_eligible: boolean; summary: Record<string, unknown>; source_summary: Record<string, unknown>; quality_flags: string[]; updated_at?: string };
type ProviderApiCapability = { provider_key: string; label: string; api_name: string; availability: string; frequency: string; decision_eligible: boolean; note?: string; verified_at?: string; last_checked_at?: string; metadata?: Record<string, unknown> };
type Sector = { taxonomy_key: string; sector_key: string; label: string; active_members: number; updated_at?: string; metadata?: Record<string, unknown> };
type SectorFlow = { taxonomy_key: string; sector_key: string; label: string; trading_date: string; close?: number; change_pct?: number; net_amount?: number; net_buy_amount?: number; net_sell_amount?: number; constituent_count?: number; leading_label?: string; provider_key: string; available_at?: string };
type ConceptSignal = { sector_key: string; label: string; change_pct?: number; net_amount?: number; net_buy_amount?: number; net_sell_amount?: number; constituent_count?: number; leading_label?: string; up_nums?: number; streak_days?: number; aggregate_score: number; flow_score: number; momentum_score: number; strength_score?: number | null; provider_key: string; strength_provider?: string | null };
type ConceptCandidate = { sector_key: string; concept_label: string; symbol: string; name?: string; limit_tag?: string; limit_type?: string; pct_change?: number; price?: number; limit_amount?: number; turnover_rate?: number; open_num?: number; status?: string; description?: string; provider_key: string; available_at?: string; membership_status?: string; board_net_amount?: number; board_change_pct?: number; board_leading_label?: string };
type Announcement = { event_id: string; symbol: string; event_type: string; occurred_at: string; available_at: string; source: string; title: string; url?: string };
type BoardStock = { symbol: string; name?: string; main_net_inflow?: number; volume_ratio?: number; turnover_rate?: number; pct_change?: number; turnover?: number };
type BoardItem = { taxonomy_key: string; sector_key: string; label: string; net_inflow?: number; change_pct?: number; mapped_members: number; quoted_members: number; top_stocks: BoardStock[]; trade_date?: string };
type BoardReviewReport = { board_report_id: string; observed_at: string; status: string; source_status?: Record<string, unknown>; summary?: Record<string, unknown>; payload?: { coverage?: Record<string, { flow_boards?: number; boards_with_members?: number; quoted_members?: number }>; items?: BoardItem[] }; created_at?: string };
type BoardFlowPoint = { observed_at: string; net_inflow: number; change_pct?: number | null };
type BoardFlowSeries = { taxonomy_key: string; sector_key: string; label: string; points: BoardFlowPoint[] };
type BoardFlowSnapshot = { observed_at: string; coverage: number; source: string };
type BoardFlowResponse = { trade_date: string; taxonomy: 'industry' | 'concept'; items: BoardFlowSeries[]; snapshots: BoardFlowSnapshot[]; cursor?: string | null; cadence_seconds: number; retention_days: number; notice: string; exchange_clock_observed_at: string; is_exchange_today: boolean; display_slots: string[]; display_start?: string | null; display_end?: string | null };
type MarketFlowFeature = { feature_key: string; exchange_date: string; cadence: 'minute' | 'midday' | 'close'; observed_at: string; source_snapshot_minute?: string | null; status: 'ready' | 'partial' | 'insufficient'; market_state: string; concept_count: number; concept_positive_ratio?: number | null; concept_median_flow?: number | null; concept_mean_change_pct?: number | null; five_minute_positive_ratio_delta?: number | null; session_positive_ratio_delta?: number | null; afternoon_repair_strength?: number | null; market_amount?: number | null; market_volume?: number | null; amount_change_pct?: number | null; volume_change_pct?: number | null; advancer_ratio?: number | null; quality_flags?: string[]; features?: Record<string, unknown> };
type SectorFlowDailyFeature = { trading_date: string; sector_key: string; label: string; provider_key: string; status: string; transition: string; net_amount?: number | null; previous_net_amount?: number | null; net_change_amount?: number | null; net_acceleration?: number | null; rank_percentile?: number | null; flow_sign_streak: number; change_pct?: number | null; price_flow_divergence?: string | null; lhb_stock_count: number; lhb_net_amount?: number | null; lhb_negative_count: number; lhb_sell_pressure_ratio?: number | null; limit_up_count: number; quality_flags?: string[] };
type SectorFlowOutcomeSummary = { transition: string; horizon_days: number; matured: number; avg_directional_return?: number | null; avg_excess_return?: number | null; directional_hit_rate?: number | null };
type MarketFlowResponse = { trade_date: string; timezone: string; items: MarketFlowFeature[]; latest?: MarketFlowFeature | null; daily?: MarketFlowFeature[]; sector_daily?: SectorFlowDailyFeature[]; sector_outcome_summary?: SectorFlowOutcomeSummary[]; state_counts?: Record<string, number>; research_gate?: { status: string; observed_trading_days?: number; matured_independent_events?: number; minimum_trading_days: number; minimum_independent_events: number; live_strategy_effect: string }; notice?: string };
type BoardRotationEvent = { rotation_event_id: string; taxonomy_key: string; sector_key: string; label: string; event_type: 'cross_zero' | 'flow_surge'; direction: 'inflow' | 'outflow'; state: 'confirming' | 'confirmed' | 'alerted' | 'expired'; first_observed_at: string; last_observed_at: string; conditions?: { previous_net_inflow?: number; current_net_inflow?: number; delta_net_inflow?: number; dynamic_threshold?: number; change_pct?: number | null }; delivery_status?: 'pending' | 'sent' | 'failed' | null; sent_at?: string | null; error_message?: string | null };
type BoardStockMiningCandidate = { rank: number; direction: 'inflow' | 'outflow'; setup_key: string; symbol: string; name?: string; label: string; score: number; board_net_inflow?: number; main_net_inflow?: number; volume_ratio?: number; turnover_rate?: number; pct_change?: number; risk_flags?: string[] };
type BoardStockMining = { run?: { observed_at?: string; status?: string; coverage?: { exact_complete_boards?: number; quoted_exact_members?: number; partial_or_unmapped_boards_skipped?: number }; summary?: { returned?: number; inflow_candidates?: number; outflow_candidates?: number; notice?: string } } | null; inflow?: BoardStockMiningCandidate[]; outflow?: BoardStockMiningCandidate[]; notice?: string };
type LimitLinkageCandidate = { rank: number; symbol: string; name?: string; score: number; shared_concepts: number; concept_labels?: string[]; leader_symbols?: string[]; leader_names?: string[]; pct_change?: number; main_net_inflow?: number; volume_ratio?: number; turnover_rate?: number; risk_flags?: string[] };
type LimitLinkageMining = { run?: { observed_at?: string; trade_date?: string; status?: string; summary?: { anchors?: number; exact_relation_rows?: number; candidate_count?: number } } | null; items?: LimitLinkageCandidate[]; notice?: string };
type BackfillState = { state: string; boards: number; members: number; latest_updated_at?: string };
type ConceptBackfill = { trade_date?: string | null; total_concepts: number; mapped_concepts: number; complete?: boolean; receipt_mapped_concepts?: number; receipt_complete?: boolean; states: BackfillState[]; mapping_evidence?: { status?: string; latest_available_at?: string | null; notice?: string }; automatic?: { enabled: boolean; batch_size: number }; notice?: string };
type IndexRegimeItem = { symbol: string; trading_date?: string; close?: number; drawdown_high_to_low_pct?: number; rebound_from_low_pct?: number; versus_period_high_pct?: number; range_retracement?: number; return_5_sessions_pct?: number; volume_ratio_5_vs_prior15?: number };
type MultiIndexRegime = { state?: string; index_count?: number; median_range_retracement?: number; interpretation?: string; items?: IndexRegimeItem[] };
type IndexBreadthContext = Record<string, unknown> & { multi_index_regime?: MultiIndexRegime; quality_flags?: string[] };
type ShortTermReview = {
  status?: string;
  methodology?: string;
  market_emotion?: { state?: string; limit_up_count?: number; limit_down_count?: number; previous_limit_count?: number; previous_limit_positive_ratio?: number | null; previous_limit_average_change_pct?: number | null; interpretation?: string };
  ladder?: { highest_board_count?: number | null; multi_board_count?: number; distribution?: Array<{ board_count?: number; count?: number }>; gaps_below_highest?: number[]; ladder_state?: string; interpretation?: string };
  sector_structure?: { inflow_leaders?: Array<Record<string, unknown>>; outflow_leaders?: Array<Record<string, unknown>>; candidate_mainlines?: Array<Record<string, unknown>>; complete_board_count?: number; coverage_note?: string };
  capital_and_lhb?: { top_amount_advancers?: number; top_amount_decliners?: number; top_amount_average_change_pct?: number | null; daily_symbol_count?: number; top_amount_evidence_status?: string; top_amount_quality_flags?: string[]; top20_amount_share?: number | null; lhb_stock_count?: number; lhb_positive_net_count?: number; lhb_negative_net_count?: number; tushare_institution_records?: number; tushare_institution_net_buy?: number | null; lhb_seat_evidence_status?: string; coverage_note?: string; top_amount_symbols?: Array<Record<string, unknown>> };
  loss_effect?: { negative_daily_count?: number; previous_limit_deep_loss_count?: number; limit_open_count?: number; intraday_reversal_count?: number; risk_flags?: string[]; largest_losses?: Array<Record<string, unknown>>; previous_limit_deep_losses?: Array<Record<string, unknown>>; intraday_reversals?: Array<Record<string, unknown>> };
  wind_flags?: Array<{ symbol?: string; name?: string; type?: string; board_count?: number; reason?: string; next_session_trigger?: string; invalidation?: string }>;
  next_session_plan?: { participation?: string; triggers?: string[]; invalidations?: string[]; symbols?: string[]; symbol_plans?: Array<{ symbol?: string; name?: string; type?: string; reason?: string; next_session_trigger?: string; invalidation?: string }>; decision_eligible?: boolean };
  notice?: string;
};
type StrategyReview = { exchange_date?: string; session?: string; observed_at?: string; market_state?: string; report?: { index_breadth_context?: IndexBreadthContext; analyst_context?: Record<string, unknown>; data_boundary?: Record<string, unknown>; short_term_review?: ShortTermReview } };
type PostCloseCandidate = { rank: number; symbol: string; name?: string; candidate_type: 'base_ready_30d' | 'base_forming_15d' | 'fresh_start_15d'; score: number; structure: { status?: string; score?: number; bar_count?: number; metrics?: Record<string, unknown>; notice?: string }; board_context: { label?: string; net_amount?: number; change_pct?: number; exact_member_mapping?: boolean }; risk_flags: string[]; discovered_at?: string; expires_at?: string | null; reason_codes?: string[]; source_snapshot?: { as_of_date?: string; model_version?: string; daily_symbols?: number; exact_board_context_symbols?: number } };
type PostCloseStrategyRun = { run_id?: string; as_of_date?: string; model_version?: string; status?: string; source_status?: Record<string, unknown>; summary?: Record<string, unknown>; updated_at?: string };
type LhbContext = { top_list_rows?: number; institution_records?: number; institution_count?: number; institution_buy?: number; institution_sell?: number; institution_net_buy?: number; institutions?: string[]; reasons?: string[] };
type StrategyPatternSample = { rank: number; symbol: string; name?: string; primary_cohort: string; cohorts: string[]; board_context: { label?: string; net_amount?: number; exact_member_mapping?: boolean }; limit_context: { tag?: string; status?: string; streak_count?: number; turnover_rate?: number; limit_pool_market_rank?: number; preopen_limit_pool_rank?: number; review_score?: number; review_tier?: string; selection_reasons?: string[]; lhb_context?: LhbContext | null }; daily_features: { low_pct?: number; close_pct?: number; volume_multiple_5d?: number; ground_to_sky_daily_shape?: boolean }; intraday_pattern: { status?: string; pattern_tags?: string[]; deep_reversal_impulse?: { time?: string } | null; deep_discount_stabilization?: { time?: string; confirmation?: string } | null; standard_ignition?: { time?: string } | null; opening_drive?: { first_four_pct_time?: string; first_eight_pct_time?: string; limit_reclaim_time?: string } | null; previous_close_reclaim?: { time?: string } | null; previous_close_acceptance?: { time?: string } | null; limit_reclaim?: { time?: string } | null }; minute_source?: string; risk_flags: string[] };
type StrategyPatternRun = { run_id?: string; as_of_date?: string; model_version?: string; status?: string; source_status?: Record<string, unknown>; summary?: Record<string, unknown>; updated_at?: string };
type ContinuationWatch = { model_version?: string; status?: string; eligible?: boolean; rank?: number; streak_count?: number; seal_to_float?: number; reason?: string; risk_flags?: string[] };
type DragonLeaderWatch = { model_version?: string; status?: string; eligible?: boolean; rank?: number; leader_rank?: number | null; streak_count?: number; review_tier?: string; market_context?: { market_state?: string; observable_limit_up_count?: number; observable_multi_board_count?: number; highest_observed_streak?: number }; theme_context?: { label?: string | null; observable_limit_up_count?: number; observable_multi_board_count?: number; net_amount?: number | null; exact_member_mapping?: boolean }; session_confirmation?: { status?: string; required?: string[] }; risk_flags?: string[] };
type LimitPoolRow = { rank: number; ts_code: string; name?: string; tag?: string; board_count?: number; status?: string; price?: number; pct_chg?: number; turnover_rate?: number; open_num?: number; limit_amount?: number; limit_up_suc_rate?: number; lu_desc?: string; volume_multiple_5d?: number; volume_multiple_20d?: number; sources?: string[]; board_context?: { label?: string; net_amount?: number; change_pct?: number } | null; lhb_context?: LhbContext | null; continuation_watch?: ContinuationWatch; dragon_leader_watch?: DragonLeaderWatch };
type LimitLadderRow = LimitPoolRow & { nums?: string | number; ladder_sources?: string[] };
type LimitPoolCoverage = { status?: string; union_count?: number; intersection_count?: number; tushare_count?: number; eastmoney_count?: number; limit_step_count?: number; multi_board_union_count?: number; tushare_only?: string[]; eastmoney_only?: string[]; local_truncation?: boolean; notice?: string };
type PostCloseRefresh = { status?: string; trade_date?: string; daily_ready?: boolean; controls_ready?: boolean; deferred_stages?: string[]; retry_hint?: string | null; finished_at?: string };
type IntradayAttribution = { model_version?: string; stage?: string; market_state?: string; sector_linkage?: string; volume_baseline?: string; microstructure_research_only?: Record<string, number | null>; microstructure_notice?: string };
type IntradayOutcome = { signal_event_id: string; horizon_key: string; direction: number; entry_observed_at: string; entry_price: number; exit_observed_at?: string | null; exit_price?: number | null; raw_return?: number | null; maximum_favorable_excursion?: number | null; maximum_adverse_excursion?: number | null; status: string; tradability: string; symbol: string; signal_key: string; signal_type: string; severity: string; state: string; score: number; observed_at: string; risk_flags: string[]; attribution?: IntradayAttribution };
type IntradayOutcomeSummary = { horizon_key: string; status: string; rows: number; avg_directional_return?: number | null; avg_mfe?: number | null; avg_mae?: number | null };
type IntradayAttributionSummary = { dimension: string; cohort: string; horizon_key: string; rows: number; matured: number; hit_rate?: number | null; avg_directional_return?: number | null; avg_mfe?: number | null; avg_mae?: number | null; payoff_ratio?: number | null; evaluation_status: string; minimum_reviewable_samples: number };
type AttributionValidationGate = { status: string; matured_unique_signals: number; trading_days: number; required_unique_signals: number; required_trading_days: number };
type AnalystReadiness = { remote_analyst_id: string; name: string; stock_claims: number; directional_stock_claims: number; neutral_stock_claims: number; settled_stock_outcomes: number; latest_claim_at?: string | null; mature: boolean; reason: string };
type AnalystScorecard = { analyst_id: string; horizon_days: number; as_of_date: string; observations: number; hit_rate?: number | null; mean_excess_return?: number | null; mean_directional_return?: number | null; calibration_score?: number | null };
type DataCoverage = { first_bar_date?: string | null; latest_bar_date?: string | null; bar_days?: number; full_cross_section_days?: number; max_symbols_on_day?: number; fundamental_symbols?: number; limit_symbols?: number; minute_symbols?: number };
type HistoricalDatasetEstimate = { dataset: string; label: string; rows: number; bytes_per_row: number; priority: string; policy: string; payload_gib: number; estimated_storage_gib: number };
type HistoryEstimate = { years: number; trading_days: number; universe_symbols: number; include_minute: boolean; estimated_storage_gib: number; datasets: HistoricalDatasetEstimate[]; policy: string; current_coverage?: DataCoverage; assumptions?: Record<string, unknown> };
type FeatureReadinessItem = { feature: string; symbols: number; rows: number; latest_date?: string | null; priority: string; coverage?: number | null; status: string };
type FeatureReadiness = { universe_key: string; universe_symbols: number; items: FeatureReadinessItem[]; decision_ready: boolean; blockers: string[] };
type ResearchOverview = { counts?: Record<string, number>; latest_snapshot?: { status: string; as_of_date: string; knowledge_cutoff: string; manifest?: Record<string, unknown> } | null; latest_market_snapshot?: MarketSnapshot | null; latest_recommendation_run?: Record<string, unknown> | null; data_coverage?: DataCoverage; history_estimate?: HistoryEstimate; feature_readiness?: FeatureReadiness };
type ProviderHealth = { provider_key: string; label: string; capability?: string; priority?: number; enabled?: boolean; consecutive_failures?: number; circuit_open_until?: string | null; last_success_at?: string | null; last_failure_at?: string | null; last_error?: string | null; last_latency_ms?: number | null; last_row_count?: number | null };
type RealtimeServiceState = 'healthy' | 'ready' | 'standby' | 'starting' | 'degraded' | 'disabled' | 'unavailable';
type RealtimeService = { key: string; label: string; role: string; state: RealtimeServiceState; configured: boolean; expected_active: boolean; cadence: string; max_age_seconds?: number | null; last_observed_at?: string | null; age_seconds?: number | null; last_success_at?: string | null; last_failure_at?: string | null; last_error?: string | null; last_latency_ms?: number | null; last_row_count?: number | null; consecutive_failures?: number; circuit_open_until?: string | null; details?: Record<string, unknown> };
type RealtimeServiceStatus = { observed_at?: string; timezone?: string; session_active?: boolean; session_reason?: string; special_window_active?: boolean; summary?: { states?: Record<string, number>; enabled_watch_count?: number; decision_path_degraded?: boolean }; items?: RealtimeService[] };
type ResearchStorage = { state?: string; allow_nonessential_high_frequency?: boolean; hot_database?: { used_bytes?: number; budget_bytes?: number }; managed?: { used_bytes?: number; budget_bytes?: number } };
type AdapterHealth = { status?: string; quant_alert_configured?: boolean; events?: number; resources?: { research_storage?: ResearchStorage } };
type IngestionRouteStatus = { job_count?: number; completed_count?: number; failed_count?: number; state?: 'completed' | 'processing' | 'stalled' | 'failed' | 'filtered' | 'awaiting_message'; latest_status?: string | null; latest_stage?: string | null; remote_batch_id?: string | null; last_updated_at?: string | null; error_class?: string | null; error_message?: string | null };
type GroupRelaySourceStatus = { key: string; tag: string; chat_name: string; target_chat_ids?: string[]; enabled?: boolean; state: string; delivery_state?: 'verified' | 'awaiting_message' | 'failed'; last_polled_at?: string | null; poll_age_seconds?: number | null; last_source_message_at?: string | null; last_forwarded_at?: string | null; last_reconciled_at?: string | null; last_message_status?: string | null; failed_count?: number; ingestion?: IngestionRouteStatus | null; last_error?: string | null };
type OAuthScopeAudit = { required_scopes?: string[]; granted_scopes?: string[]; missing_scopes?: string[]; verified?: boolean | null };
type SummaryListenerStatus = { enabled?: boolean; chat_configured?: boolean; state?: string; interval_seconds?: number; last_tick_completed_at?: string | null; last_success_at?: string | null; last_source_message_at?: string | null; poll_age_seconds?: number | null; processed_count?: number; duplicate_count?: number; ignored_count?: number; last_error?: string | null };
type GroupRelayStatus = { status?: string; observed_at?: string; enabled?: boolean; interval_seconds?: number; stale_after_seconds?: number; user_oauth_configured?: boolean; user_oauth_scope_audit?: OAuthScopeAudit | null; target_configured?: boolean; delivery_verified?: boolean; last_tick_started_at?: string | null; last_tick_completed_at?: string | null; last_tick_error?: string | null; sources?: GroupRelaySourceStatus[]; summary_listener?: SummaryListenerStatus };
type GroupRelayRouteForm = { key: string; chat_name: string; chat_id: string; tag: string; target_chat_ids_text: string; target_chat_names_text: string; enabled: boolean };
type FeishuCapability = { key: string; label: string; category: string; enabled: boolean; configured: boolean; resource_configured?: boolean; implementation_ready?: boolean; authorization_subject?: 'user' | 'tenant'; authorization_status?: 'verified' | 'missing' | 'unknown' | 'awaiting_verification' | 'not_required'; missing_user_scopes?: string[]; missing_tenant_scopes?: string[]; requires: string[]; note: string };
type FeishuEventSubscription = { event_type: string; label: string; required_for: string; handler_registered: boolean; state: 'received' | 'awaiting_callback'; received_count?: number; last_received_at?: string | null };
type FeishuApplicationInspection = { status?: 'not_checked' | 'verified' | 'missing_inspection_scope' | 'error'; checked_at?: string | null; scopes?: string[]; app_status?: number | null; target_chat?: { status?: 'not_checked' | 'not_configured' | 'verified' | 'error'; message?: string }; message?: string };
type FeishuWorkbenchStatus = { target_configured?: boolean; public_h5_url?: string | null; user_oauth_configured?: boolean; user_oauth_scopes?: string; user_oauth_scope_audit?: OAuthScopeAudit | null; application_inspection?: FeishuApplicationInspection; capabilities?: FeishuCapability[]; event_subscriptions?: FeishuEventSubscription[] };
type FeishuWorkbenchMessage = { source_message_id: string; route_tag: string; source_chat_name?: string | null; message?: { msg_type?: string; body?: { content?: string } }; status: string; workflow_state?: string; workflow_note?: string | null; source_deleted?: boolean; source_create_time?: number; forwarded_at?: string | null; updated_at?: string | null; action_card_message_id?: string | null };
type RemoteReport = { remote_report_id: string; analyst_name: string; report_date: string; title: string; summary: string; remote_version?: string };
type RemoteMessage = { remote_message_id: string; remote_analyst_id: string; analyst_name: string; source_type: string; received_at: string; strategy_available_at?: string; source_published_at?: string | null; stated_at?: string | null; stated_precision?: string | null; content: string };
type AnalystSkillProfile = { remote_analyst_id: string; as_of_date: string; status: string; profile?: { language_style?: { report_count?: number; message_count?: number; author_timed_actions?: number }; skill_score?: { status?: string; mature_actions?: number; required_actions?: number; trading_days?: number; required_trading_days?: number }; point_in_time_integrity?: { factor_eligible_actions?: number; replay_only_actions?: number } } };
type AnalystObservation = { analyst_id: string; source_kind: string; strategy_available_at?: string; scope: string; subject_label?: string; action: string; status: string; confidence?: number; evidence_span?: string };
type AnalystPromptLab = { candidates?: Record<string, unknown>[]; evaluations?: Record<string, unknown>[]; intraday_outcomes?: Record<string, unknown>[]; live_effect?: string; boundary?: string };
type StrategyAblation = { items?: Record<string, unknown>[]; run?: Record<string, unknown> | null; live_effect?: string; notice?: string };
type AnalystResearchStatus = { latest_expert_run?: { status?: string; as_of_date?: string } | null; latest_research_run?: { status?: string; as_of_date?: string } | null; opinion_status_counts?: { factor_status: string; count: number }[]; approved_theme_board_aliases?: number; boundary?: string };
type AnalystMarketEvaluation = { window?: { start_date?: string; end_date?: string; timezone?: string }; analysts?: { analyst_id: string; observations: number; eligible_observations: number; replay_only_observations: number; directional_claims: number; matured_outcomes: number; pending_outcomes: number; unavailable_outcomes: number; intraday_matured_events?: number; intraday_matured_outcomes?: number; intraday_directional_hit_rate?: number | null; intraday_mean_directional_return?: number | null; manual_review_status?: string; mean_directional_return?: number | null; directional_hit_rate?: number | null; mature?: boolean; status?: string; gate_reason?: string | null }[]; timeline?: { date: string; market_state?: string | null; market_status?: string; concept_positive_ratio?: number | null; analyst_claims: number; positive_claims: number; negative_claims: number; aligned_claims: number; contrarian_claims: number }[]; sector_context?: { sector_key: string; label?: string; days: number; positive_days: number; negative_days: number; net_amount_sum: number; lhb_negative_sum: number }[]; coverage_matrix?: { analyst_id: string; scope: string; observations: number; eligible_observations: number; replay_only_observations: number; unmapped_observations: number; neutral_observations: number; opinions: number; directional_opinions: number; matured_outcomes: number; pending_outcomes: number; unavailable_outcomes: number; hit_rate?: number | null; mean_directional_return?: number | null }[]; horizon_matrix?: { analyst_id: string; scope: string; horizon_days: number; outcomes: number; matured: number; pending: number; unavailable: number; hit_rate?: number | null; mean_directional_return?: number | null }[]; intraday_outcomes?: { horizon_minutes: number; matured: number; pending: number; unavailable: number; mean_directional_return?: number | null; hit_rate?: number | null; mean_mfe?: number | null; mean_mae?: number | null }[]; intraday_action_outcomes?: { action: string; horizon_minutes: number; matured: number; pending: number; unavailable: number; mean_directional_return?: number | null; hit_rate?: number | null; mean_mfe?: number | null; mean_mae?: number | null }[]; author_action_outcomes?: { analyst_id: string; action: string; horizon_minutes: number; matured: number; pending: number; unavailable: number; mean_directional_return?: number | null; hit_rate?: number | null; replay_only?: boolean }[]; calibration?: { status?: string; events?: number; event_dates?: number; oof_events?: number; model?: { brier?: number | null; log_loss?: number | null }; baseline?: { brier?: number | null; log_loss?: number | null }; live_effect?: string } ; baselines?: Record<string, { observations?: number; hit_rate?: number | null; mean_directional_return?: number | null }>; quality_gate?: { status?: string; observed_trading_days?: number; matured_independent_events?: number; matured_daily_independent_events?: number; matured_intraday_independent_events?: number; minimum_trading_days?: number; minimum_independent_events?: number; live_strategy_effect?: string; notice?: string }; event_ledger?: { observations?: number; opinions?: number; outcomes?: number; intraday_outcomes?: number; matured_independent_events?: number; matured_daily_independent_events?: number; matured_intraday_independent_events?: number; append_only_source?: string }; analyst_id?: string | null };
type AnalystStockTimeline = { symbol: string; start_date: string; end_date: string; timezone: string; bar_source?: string; bar_count: number; action_count: number; bars: { bar_time: string; open: number; high: number; low: number; close: number; volume?: number | null; amount?: number | null; source_name?: string }[]; actions: { event_id: string; analyst_id: string; symbol: string; label?: string; action: string; direction: number; event_time?: string | null; stated_at?: string | null; available_at?: string | null; evidence?: string; source_kind?: string; replay_only?: boolean; time_basis?: string; mapping_status?: string; nearest_bar_time?: string; nearest_bar_close?: number; offset_seconds?: number }[]; boundary?: string };
type AnalystClaim = { claim_id: string; analyst_name: string; scope: string; subject_label?: string; direction: number; strength: number; horizon_days: number; extraction_confidence?: number; direction_source?: string; evidence: string; available_at?: string };
type Recommendation = { rank: number; symbol: string; decision: string; score: number; direction?: number; horizon_days?: number; confidence?: number; risk_flags?: string[]; explanation?: Record<string, unknown>; score_breakdown?: Record<string, unknown>; valid_until?: string };
type UniverseMember = { symbol: string; name?: string; industry?: string; enabled: boolean; priority: number; source?: string };
type FeatureItem = { symbol: string; name?: string; features: Record<string, unknown>; quality_flags: string[] };
type ClaimReview = { review_id: string; suggested_label: string; suggested_symbol?: string; analyst_name?: string; direction: number; strength: number; horizon_days: number; evidence: string; status: string };
type QualityIssue = { issue_id?: string; severity: string; capability?: string; symbol?: string; trading_date?: string; code: string; message: string; created_at?: string };
type MinuteImport = { import_id: string; source_name: string; file_name: string; status: string; row_count: number; rejected_rows: number; started_at?: string; finished_at?: string; error_message?: string };
type StudySource = { source: string; api_name: string; provider?: string; status: string; received: number; stored: number; error?: string; failures?: string[]; fallback_failures?: { provider: string; error: string }[] };
type StudyClaim = { claim_id: string; analyst_name: string; subject_label?: string; direction: number; strength: number; horizon_days: number; extraction_confidence?: number; available_at?: string; evidence: string };
type StockReadinessItem = { api_name: string; label: string; priority: string; rows: number; latest_date?: string | null; status: string };
type StockReadiness = { symbol: string; window_start: string; window_end: string; mode: string; decision_ready: boolean; blockers: string[]; items: StockReadinessItem[] };
type StockStudy = { symbol: string; as_of_date: string; lookback_days: number; sources: StudySource[]; on_demand_readiness?: StockReadiness; market: Record<string, Record<string, unknown> | Record<string, unknown>[] | null>; events?: { announcements?: Announcement[]; provider?: string; decision_eligible?: boolean }; technical: Record<string, unknown>; analyst: { summary: Record<string, unknown>; claims: StudyClaim[] }; combined: { score: number; stance: string; notice: string; reasons: string[] } };
type Factor = { factor_key: string; label: string; category: string; implementation: string; framework_tags: string[]; status: string; version: string; metadata?: Record<string, unknown> };
type FactorEvaluation = { evaluation_id: string; factor_key: string; label: string; status: string; observations: number; cross_section_days: number; horizon_days: number; metrics: Record<string, unknown>; artifact?: Record<string, unknown>; created_at?: string };
type Strategy = { strategy_key: string; label: string; engine: string; version: string; configuration: Record<string, unknown>; status: string };
type StrategyExperiment = { strategy_experiment_id: string; strategy_key: string; label: string; status: string; metrics: Record<string, unknown>; parameters: Record<string, unknown>; equity_curve: { date: string; equity: number; return: number; positions: number }[]; trades: Record<string, unknown>[]; created_at?: string };
type Framework = { framework_key: string; label: string; role: string; integration_mode: string; status: string; license_note: string; prerequisites: string[] };
type TrainingRoadmap = { status: string; policy: string; stages: { stage: string; gate: string; compute: string }[] };
type PaperPortfolio = { as_of?: string; equity?: number; gross_exposure?: number; net_exposure?: number; drawdown?: number; payload?: { sector_exposure?: Record<string, number> } };
type PaperStatus = { mode?: string; live_orders?: boolean; decisions?: Record<string, unknown>[]; positions?: Record<string, unknown>[]; latest_portfolio?: PaperPortfolio | null; risk_events?: Record<string, unknown>[]; boundary?: string };
type StrategyFunnel = { funnel?: Record<string, number>; episodes?: Record<string, unknown>[]; boundary?: string };
type StrategyGovernance = { trials?: Record<string, unknown>[]; contracts?: Record<string, unknown>[]; replay_runs?: Record<string, unknown>[]; probability_calibrations?: Record<string, unknown>[]; live_effect?: string; promotion_boundary?: string };
type StrategyHealth = { status?: string; trigger_frequency?: { signals_7d?: number; signals_prior_7d?: number; episodes_7d?: number; drift_ratio?: number | null; drift_status?: string }; outcomes_30m?: { matured?: number; trading_days?: number; rows?: number; positive_rate?: number | null; avg_directional_return?: number | null }; data_freshness?: { status?: string; quote_age_seconds?: number | null; fresh_quote_rows?: number }; validation_gate?: { status?: string; required_matured_signals?: number; required_trading_days?: number; live_effect?: string }; governance_recommendation?: { action?: string; flags?: string[]; live_effect?: string; notice?: string }; strategy_breakdown?: { strategy_key: string; signals: number; episodes: number }[]; notice?: string };

const initialPath = window.location.pathname;
const mobileMediaQuery = window.matchMedia('(max-width: 760px)');
const mobileLayout = ref(mobileMediaQuery.matches);
const syncMobileLayout = (event: MediaQueryListEvent) => { mobileLayout.value = event.matches; };
const activeSection = ref(initialPath === '/relay' ? 'relay' : initialPath === '/monitor' ? 'monitor' : initialPath === '/workbench' ? 'workbench' : 'research');
const sharedResearchParams = new URLSearchParams(window.location.search);
const sharedResearchSymbol = (sharedResearchParams.get('symbol') || '').toUpperCase();
const sharedResearchTab = sharedResearchParams.get('tab');
const activeResearchTab = ref(sharedResearchTab === 'stock-study' && /^\d{6}\.(SH|SZ|BJ)$/.test(sharedResearchSymbol) ? 'stock-study' : 'overview');
const routes = ref<Route[]>([]); const events = ref<EventItem[]>([]); const connected = ref(false); const eventFilter = ref('all');
const relayTag = ref(''); const relaySource = ref(''); const relayText = ref(''); const relayFiles = ref<File[]>([]); const relayDate = ref(''); const relayTime = ref(''); const relayState = ref(''); const relayProgress = ref(0); const relayXhr = ref<XMLHttpRequest | null>(null);
const loading = ref(false); const actionLoading = ref(''); const researchError = ref('');
const overview = ref<ResearchOverview>({}); const reports = ref<RemoteReport[]>([]); const remoteMessages = ref<RemoteMessage[]>([]); const analystSkills = ref<AnalystSkillProfile[]>([]); const analystResearchStatus = ref<AnalystResearchStatus>({}); const claims = ref<AnalystClaim[]>([]); const providerHealth = ref<ProviderHealth[]>([]); const providerApiCapabilities = ref<ProviderApiCapability[]>([]); const marketSnapshots = ref<MarketSnapshot[]>([]); const sectors = ref<Sector[]>([]); const sectorFlows = ref<SectorFlow[]>([]); const conceptSignals = ref<ConceptSignal[]>([]); const conceptCandidates = ref<ConceptCandidate[]>([]); const announcements = ref<Announcement[]>([]); const lhbEvents = ref<Announcement[]>([]); const closeBoardReport = ref<BoardReviewReport | null>(null); const conceptBackfill = ref<ConceptBackfill>({ total_concepts: 0, mapped_concepts: 0, states: [] }); const closeStrategyReview = ref<StrategyReview | null>(null); const postCloseStrategyRun = ref<PostCloseStrategyRun | null>(null); const postCloseCandidates = ref<PostCloseCandidate[]>([]); const strategyPatternRun = ref<StrategyPatternRun | null>(null); const strategyLimitPool = ref<LimitPoolRow[]>([]); const strategyLimitLadder = ref<LimitLadderRow[]>([]); const strategyContinuationCandidates = ref<LimitPoolRow[]>([]); const strategyDragonLeaderCandidates = ref<LimitPoolRow[]>([]); const strategyDragonLeaderMarket = ref<DragonLeaderWatch['market_context']>({}); const strategyPoolCoverage = ref<LimitPoolCoverage>({}); const strategyPatternPicks = ref<StrategyPatternSample[]>([]); const strategyPatternSamples = ref<StrategyPatternSample[]>([]); const postCloseRefresh = ref<PostCloseRefresh | null>(null); const intradayOutcomes = ref<IntradayOutcome[]>([]); const intradayOutcomeSummary = ref<IntradayOutcomeSummary[]>([]); const intradayAttributionSummary = ref<IntradayAttributionSummary[]>([]); const attributionValidationGate = ref<AttributionValidationGate>({ status: 'accumulating', matured_unique_signals: 0, trading_days: 0, required_unique_signals: 200, required_trading_days: 60 }); const analystReadiness = ref<AnalystReadiness[]>([]); const analystScorecards = ref<AnalystScorecard[]>([]); const selectedReviewBoardKey = ref(''); const catalog = ref<{ count?: number; counts?: CatalogCounts; items?: CatalogItem[]; providers?: ProviderConfig[]; online_range_max_days?: number; historical_minute_policy?: string; realtime_minute_policy?: string; coverage_rule?: string }>({}); const recommendations = ref<Recommendation[]>([]); const universe = ref<UniverseMember[]>([]); const featureItems = ref<FeatureItem[]>([]); const claimReviews = ref<ClaimReview[]>([]); const factors = ref<Factor[]>([]); const factorEvaluations = ref<FactorEvaluation[]>([]); const strategies = ref<Strategy[]>([]); const strategyExperiments = ref<StrategyExperiment[]>([]); const mainWaveExperiments = ref<StrategyExperiment[]>([]); const frameworks = ref<Framework[]>([]); const trainingRoadmap = ref<TrainingRoadmap>({ status: 'planned', policy: '', stages: [] }); const qualityIssues = ref<QualityIssue[]>([]); const minuteImports = ref<MinuteImport[]>([]); const minuteDirectory = ref('');
const realtimeServices = ref<RealtimeServiceStatus>({ items: [] }); const adapterHealth = ref<AdapterHealth>({}); const runtimeHealth = ref<{ resources?: { research_storage?: ResearchStorage }; network?: { state?: string; consecutive_failures?: number; last_success_at?: string | null; last_failure_at?: string | null; last_source?: string | null; last_error?: string | null; recovery_count?: number }; daily_control_plane?: { state?: string; trade_date?: string; daily_rows?: number; adjustment_rows?: number; limit_rows?: number; reason?: string | null } }>({}); const realtimeLoading = ref(false); const realtimeError = ref('');
const groupRelayStatus = ref<GroupRelayStatus>({ sources: [] }); const groupRelayLoading = ref(false); const groupRelayError = ref('');
const groupRelayRouteDialog = ref(false); const groupRelayRouteSaving = ref(false); const groupRelayRouteForm = ref<GroupRelayRouteForm>({ key: '', chat_name: '', chat_id: '', tag: '', target_chat_ids_text: '', target_chat_names_text: '', enabled: true });
const feishuWorkbench = ref<FeishuWorkbenchStatus>({ capabilities: [] }); const feishuWorkbenchMessages = ref<FeishuWorkbenchMessage[]>([]); const feishuWorkbenchLoading = ref(false); const feishuWorkbenchError = ref(''); const feishuWorkbenchAction = ref(''); const workbenchSearch = ref(''); const workbenchSearchResult = ref<unknown>(null); const workbenchIntegrationDialog = ref(false); const workbenchIntegration = ref({ kind: 'documents', title: '新建飞书文档', payloadText: '{\n  "title": "群消息研究笔记"\n}' });
const paperStatus = ref<PaperStatus>({});
const analystObservations = ref<AnalystObservation[]>([]);
const strategyFunnel = ref<StrategyFunnel>({});
const strategyGovernance = ref<StrategyGovernance>({});
const analystSyncHealth = ref<{ cursors?: Record<string, unknown>[]; stream_health?: { stream_key: string; status: string; cursor_count: number; age_seconds?: number | null; notice?: string | null; latest_attempt_summary?: { transport?: { requests?: number; retries?: number; status_counts?: Record<string, number> } } | null }[]; workflow_health?: { id: string; active?: boolean; published?: boolean; status?: string; execution_evidence?: string; latest_execution_status?: string | null; latest_started_at?: string | null; latest_stopped_at?: string | null; smoke_execution_status?: string | null; smoke_execution_at?: string | null; notice?: string | null }[]; promotion_registry?: Record<string, unknown>[]; live_effect?: string; runtime_verification?: string }>({});
const analystPromptLab = ref<AnalystPromptLab>({});
const analystMarketEvaluation = ref<AnalystMarketEvaluation>({});
const analystDailyReview = ref<AnalystMarketReview | null>(null); const analystWeeklyReview = ref<AnalystMarketReview | null>(null); const analystReviewRunning = ref('');
const analystReviewRuns = ref<AutomationRun[]>([]);
const automationRuns = ref<AutomationRun[]>([]);
const analystStockTimeline = ref<AnalystStockTimeline | null>(null); const analystStockTimelineLoading = ref(false); const analystStockTimelineError = ref(''); const analystTimelineAnalyst = ref(''); const analystTimelineDate = ref('');
const strategyAblation = ref<StrategyAblation>({});
const strategyHealth = ref<StrategyHealth>({});
const catalogQuery = ref(''); const catalogGroup = ref('all'); const selectedCatalog = ref<CatalogItem[]>([]); const auditResults = ref<CapabilityAuditRow[]>([]); const catalogRefreshing = ref(false); const fetchDialogOpen = ref(false); const fetchResultOpen = ref(false); const fetchResult = ref<Record<string, unknown>>({}); const fetchForm = ref({ api_name: 'daily', provider: 'auto', paramsText: '{\n  "ts_code": "000001.SZ",\n  "start_date": "20260804",\n  "end_date": "20260804"\n}', fields: 'ts_code,trade_date,open,high,low,close,vol,amount', max_rows: 100 });
const studySymbol = ref(/^\d{6}\.(SH|SZ|BJ)$/.test(sharedResearchSymbol) ? sharedResearchSymbol : '000636.SZ'); const studyLookback = ref(21); const stockStudy = ref<StockStudy | null>(null); const studyLoading = ref(false); const studyError = ref('');
const universeText = ref(''); const universePriority = ref(100); const reviewSymbol = ref<Record<string, string>>({}); const sectorMemberOffset = ref(0); const sectorMemberLimit = ref(10); const sectorFlowDate = ref('');
const chinaMinute = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '-';
const chinaDateTime = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '-';
const boardFlowTaxonomy = ref<'industry' | 'concept'>('industry'); const boardFlowDate = ref(''); const boardFlowSeries = ref<Record<string, BoardFlowSeries>>({}); const boardFlowSnapshots = ref<BoardFlowSnapshot[]>([]); const boardFlowCursor = ref<string | null>(null); const boardFlowLoading = ref(false); const boardFlowError = ref(''); const boardFlowNotice = ref(''); const boardFlowFocus = ref<string[]>([]); const boardFlowDisplaySlots = ref<string[]>([]); const boardFlowIsExchangeToday = ref(false); const boardRotationEvents = ref<BoardRotationEvent[]>([]); const boardStockMining = ref<BoardStockMining>({}); const limitLinkageMining = ref<LimitLinkageMining>({});
const marketFlow = ref<MarketFlowResponse>({ trade_date: '', timezone: 'Asia/Shanghai', items: [] }); const marketFlowError = ref('');
const selectedFactors = ref<string[]>([]); const factorHorizon = ref(5); const backtestForm = ref({ rebalance_days: 5, hold_days: 5, top_n: 20, total_cost_bps: 18 });
let retryTimer: number | undefined; let retryDelay = 1000; let eventSource: EventSource | undefined;
const polling = usePolling();

const visibleEvents = computed(() => eventFilter.value === 'all' ? events.value : events.value.filter((item) => item.n8n_status === eventFilter.value));
const catalogGroups = computed(() => ['all', ...Array.from(new Set((catalog.value.items ?? []).map((item) => item.group)))]);
const visibleCatalog = computed(() => (catalog.value.items ?? []).filter((item) => (catalogGroup.value === 'all' || item.group === catalogGroup.value) && (!catalogQuery.value || `${item.api_name} ${item.group} ${item.model_role} ${item.request_policy}`.toLowerCase().includes(catalogQuery.value.toLowerCase()))));
const count = (name: string) => overview.value.counts?.[name] ?? 0;
const dateText = (value?: string | null) => value ? new Date(value).toLocaleString() : '未运行';
const healthState = (provider: ProviderHealth) => provider.circuit_open_until ? 'danger' : provider.last_error ? 'warning' : provider.last_success_at ? 'success' : 'info';
const realtimeStateType = (state?: RealtimeServiceState): 'success' | 'warning' | 'danger' | 'info' => state === 'healthy' || state === 'ready' ? 'success' : state === 'starting' || state === 'standby' ? 'warning' : state === 'degraded' || state === 'disabled' ? 'danger' : 'info';
const realtimeStateText = (state?: RealtimeServiceState) => ({ healthy: '运行正常', ready: '投递就绪', standby: '待命', starting: '启动中', degraded: '降级/延迟', disabled: '未配置', unavailable: '明确不可用' }[state ?? 'disabled']);
const groupRelayStateType = (state?: string): 'success' | 'warning' | 'danger' | 'info' => state === 'healthy' ? 'success' : ['starting', 'delayed'].includes(state ?? '') ? 'warning' : ['error', 'unavailable', 'degraded', 'not_configured'].includes(state ?? '') ? 'danger' : 'info';
const groupRelayStateText = (state?: string) => ({ healthy: '正常监听', starting: '建立基线中', delayed: '轮询延迟', degraded: '有失败待重试', error: '读取异常', unavailable: '群不可读', not_configured: '授权未配置', disabled: '已停用' }[state ?? 'starting'] ?? state ?? '未知');
const groupRelayMessageText = (status?: string | null) => ({ sent: '已转发', skipped_bootstrap: '历史基线', filtered_system: '系统消息已过滤', processing: '转发中', failed: '转发失败', unsupported: '不支持' }[status ?? ''] ?? '暂无消息');
const realtimeDeliveryDetail = (service: RealtimeService) => {
  const details = service.details ?? {};
  if (service.key === 'tencent_realtime' && details.public_flow_snapshot) {
    const snapshot = details.public_flow_snapshot as { status?: string; age_seconds?: number; max_decision_age_seconds?: number; decision_eligible?: boolean };
    return `资金流 ${snapshot.status ?? '未知'}；${snapshot.decision_eligible ? '可用于新入场确认' : '仅展示，禁止资金流确认'}；${ageText(snapshot.age_seconds)} / ${ageText(snapshot.max_decision_age_seconds)}`;
  }
  if (service.key === 'feishu_alert') return `最近 ${details.latest_delivery_kind ?? '无'} / ${details.latest_delivery_status ?? '无'}；待重试 ${details.pending_retry_count ?? 0}；带外关注 ${details.meta_alert_state ?? 'normal'}`;
  if (service.key === 'daily_strategy_summary') return `最近交易日 ${details.latest_exchange_date ?? '尚无'}；投递 ${details.latest_delivery_status ?? '尚无'}；尝试 ${details.attempt_count ?? 0}/3`;
  return '';
};
const ageText = (seconds?: number | null) => seconds === null || seconds === undefined ? '-' : seconds < 60 ? `${Math.round(seconds)} 秒` : seconds < 3600 ? `${(seconds / 60).toFixed(1)} 分钟` : `${(seconds / 3600).toFixed(1)} 小时`;
const bytesText = (bytes?: number | null) => bytes === null || bytes === undefined ? '-' : bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${bytes} B`;
const paperSectorExposureItems = () => Object.entries(paperStatus.value.latest_portfolio?.payload?.sector_exposure ?? {}).sort((left, right) => right[1] - left[1]).slice(0, 12).map(([sector, value]) => ({ sector, value }));
const claimDirection = (value: number) => value > 0 ? '偏多' : value < 0 ? '偏空' : '中性';
const studyStance = (value?: string) => value === 'research_positive' ? '研究偏正面' : value === 'research_negative' ? '研究偏负面' : '证据混合或不足';
const studyType = (value?: string) => value === 'research_positive' ? 'success' : value === 'research_negative' ? 'danger' : 'info';
const recommendationDirection = (value?: number) => value && value > 0 ? '偏多' : value && value < 0 ? '偏空' : '中性';
const recommendationType = (value?: number) => value && value > 0 ? 'success' : value && value < 0 ? 'danger' : 'info';
const postCloseCandidateLabel = (value: PostCloseCandidate['candidate_type']) => value === 'base_ready_30d' ? '30日蓄势就绪' : value === 'base_forming_15d' ? '15日形成中' : '15日首动';
const postCloseCandidateType = (value: PostCloseCandidate['candidate_type']) => value === 'base_ready_30d' ? 'success' : value === 'fresh_start_15d' ? 'warning' : 'info';
const patternCohortLabel = (value: string) => ({ focus: '重点研究', dragon_leader_watch: '龙头复核', limit_continuation_watch: '连板延续', ground_to_sky: '地天反转', preopen_market_leader: '盘前辨识度', market_leader: '盘后辨识度', board_leader: '板块龙头', consecutive_limit: '连板梯队', first_board: '首板' }[value] ?? value);
const sourceType = (value?: string) => value === 'completed' || value === 'unchanged' ? 'success' : value === 'partial' ? 'warning' : value === 'failed' ? 'danger' : 'info';
const snapshotType = (value?: string) => value === 'ready' ? 'success' : value === 'degraded' ? 'warning' : value === 'blocked' || value === 'failed' ? 'danger' : 'info';
const availabilityType = (value?: Availability): 'success' | 'warning' | 'danger' | 'info' => value === 'verified' ? 'success' : value === 'empty' || value === 'declared' ? 'warning' : value === 'failed' || value === 'unsupported' ? 'danger' : 'info';
const availabilityText = (value?: Availability) => ({ verified: '已验证', empty: '有效空值', declared: '待验证', unsupported: '明确拒绝', failed: '调用失败', unknown: '未登记' }[value ?? 'unknown']);
const permissionText = (item: CatalogItem) => item.permission_model === 'points' ? `${item.min_points ?? '-'} 积分` : item.permission_model === 'separate_permission' ? '独立权限' : item.permission_model === 'offline_delivery' ? '离线交付' : '供应商合同';
const policyText = (value: string) => value === 'market_hours_only' ? '仅交易时段' : value === 'offline_files_only' ? '仅离线文件' : '在线受控';
const catalogCount = (name: keyof CatalogCounts) => catalog.value.counts?.[name] ?? 0;
const observationText = (item: CatalogItem, provider: 'tushare_primary' | 'tushare_super_sdk' | 'tushare_super_get') => dateText(item.provider_observations?.[provider]?.last_checked_at ?? item.provider_observations?.[provider]?.verified_at);
const displayValue = (value: unknown) => value === null || value === undefined || value === '' ? '-' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const readinessType = (value: number) => value > 0 ? 'warning' : 'success';
const outcomeStatusType = (value: string) => value === 'matured' ? 'success' : value === 'pending' ? 'warning' : 'info';
const outcomePercent = (value?: number | null) => value === undefined || value === null || !Number.isFinite(Number(value)) ? '-' : `${(Number(value) * 100).toFixed(2)}%`;
const moneyWan = (value?: number | null) => value === undefined || value === null || !Number.isFinite(Number(value)) ? '-' : `${(Number(value) / 10_000).toFixed(0)}万`;
const reviewTierText = (value?: string) => value === 'priority_review' ? '优先复核' : value === 'candidate_review' ? '候选复核' : '研究样本';
const attributionDimensionLabel = (value: string) => ({ model_version: '模型版本', stage: '信号阶段', market_state: '市场环境', sector_linkage: '板块联动', volume_baseline: '同刻量能', microstructure_state: '盘口状态', price_volume_state: '量价相关', smart_money_state: '高信息量价' }[value] ?? value);
const attributionCohortLabel = (value: string) => ({ acceptance: '承接确认', expansion: '首动扩张', extension_watch: '延伸观察', risk_exit: '风控退出', generic: '通用信号', rotation_defensive: '防御/资源轮动', rotation_technology: '科技轮动', broad_risk_on: '广泛偏强', broad_risk_off: '广泛偏弱', mixed_or_neutral: '混合/中性', peer_and_board_top10_confirmed: '同伴+板块Top10', peer_confirmed: '同伴联动确认', board_top10_positive: '正流入板块Top10', board_top10_nonpositive: '非正流入板块Top10', peers_not_confirmed: '同伴未确认', unobserved: '未观察到联动', ready: '基线可用', insufficient: '基线不足', not_applicable: '不适用' }[value] ?? value);
const attributionStatusType = (value: string): 'success' | 'warning' | 'info' => value === 'cohort_reviewable' ? 'success' : value === 'descriptive_only' ? 'warning' : 'info';
const analystReadinessText = (value: string) => ({ no_directional_stock_claims: '缺少方向明确的股票观点', fewer_than_30_settled_stock_outcomes: '已结算样本少于30条', eligible_for_scorecard_review: '达到成绩单复核门槛' }[value] ?? value);
const historyDatasetRows = computed(() => overview.value.history_estimate?.datasets.slice(0, 8) ?? []);
const featureReadinessRows = computed(() => overview.value.feature_readiness?.items ?? []);
const storageText = (value?: number) => value === undefined || value === null ? '-' : `${Number(value).toFixed(2)} GiB`;
const rowText = (value?: number) => value === undefined || value === null ? '-' : Number(value).toLocaleString();
const featureStatusType = (value?: string) => value === 'ready' ? 'success' : value === 'missing' ? 'danger' : 'warning';
const studyBars = computed<Record<string, unknown>[]>(() => {
  const bars = stockStudy.value?.market.daily_bars;
  return Array.isArray(bars) ? bars : [];
});
const studyMarketRecord = (name: string): Record<string, unknown> => {
  const value = stockStudy.value?.market[name];
  return value && !Array.isArray(value) ? value : {};
};
const featureRecord = (row: FeatureItem, name: string): Record<string, unknown> => {
  const value = row.features[name];
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
};
const metricNumber = (metrics: Record<string, unknown>, name: string, digits = 3) => {
  const raw = Number(metrics[name]); return Number.isFinite(raw) ? raw.toFixed(digits) : '-';
};
const nestedValue = (record: Record<string, unknown> | undefined, path: string) => path.split('.').reduce<unknown>((value, key) => value && typeof value === 'object' ? (value as Record<string, unknown>)[key] : undefined, record);
const nestedNumber = (record: Record<string, unknown> | undefined, path: string, digits = 4) => {
  const raw = Number(nestedValue(record, path)); return Number.isFinite(raw) ? raw.toFixed(digits) : '-';
};
const latestFactorEvaluations = computed(() => {
  const seen = new Set<string>();
  return factorEvaluations.value.filter((item) => {
    if (seen.has(item.factor_key)) return false;
    seen.add(item.factor_key); return true;
  });
});
const factorChartOption = computed(() => ({
  tooltip: { trigger: 'axis' }, legend: { data: ['全样本中性IC', '样本外IC'] }, grid: { left: 48, right: 18, top: 42, bottom: 62 }, xAxis: { type: 'category', data: latestFactorEvaluations.value.map((item) => item.label || item.factor_key), axisLabel: { rotate: 35 } }, yAxis: { type: 'value', name: 'Rank IC' }, series: [
    { name: '全样本中性IC', type: 'bar', data: latestFactorEvaluations.value.map((item) => Number(nestedValue(item.metrics, 'neutral_rank_ic.mean')) || 0), itemStyle: { color: '#1976d2' } },
    { name: '样本外IC', type: 'bar', data: latestFactorEvaluations.value.map((item) => Number(nestedValue(item.metrics, 'walk_forward.test.neutral_rank_ic.mean')) || 0), itemStyle: { color: '#ef6c00' } },
  ],
}));
const latestExperiment = computed(() => strategyExperiments.value[0] ?? null);
const latestMainWaveExperiment = computed(() => mainWaveExperiments.value.find((item) => item.strategy_key === 'watchlist_main_wave_shadow_v2') ?? null);
const latestReboundExperiment = computed(() => mainWaveExperiments.value.find((item) => item.strategy_key === 'watchlist_countertrend_rebound_shadow_v1') ?? null);
const mainWaveCurrentScores = computed<Record<string, unknown>[]>(() => {
  const value = nestedValue(latestMainWaveExperiment.value?.metrics, 'current_scores');
  return Array.isArray(value) ? value as Record<string, unknown>[] : [];
});
const mainWaveQualification = computed(() => {
  const value = nestedValue(latestMainWaveExperiment.value?.metrics, 'pattern_summary.qualification');
  return value && typeof value === 'object' ? Object.entries(value as Record<string, unknown>).map(([key, threshold]) => ({ key, threshold })) : [];
});
const mainWaveFailedChecks = computed(() => {
  const value = nestedValue(latestMainWaveExperiment.value?.metrics, 'promotion_gate.checks');
  return value && typeof value === 'object' ? Object.entries(value as Record<string, unknown>).filter(([, passed]) => passed !== true).map(([key]) => key) : [];
});
const reboundCurrentScores = computed<Record<string, unknown>[]>(() => {
  const value = nestedValue(latestReboundExperiment.value?.metrics, 'current_scores');
  return Array.isArray(value) ? value as Record<string, unknown>[] : [];
});
const reboundFailedChecks = computed(() => {
  const value = nestedValue(latestReboundExperiment.value?.metrics, 'promotion_gate.checks');
  return value && typeof value === 'object' ? Object.entries(value as Record<string, unknown>).filter(([, passed]) => passed !== true).map(([key]) => key) : [];
});
const reviewConceptBoards = computed(() => (closeBoardReport.value?.payload?.items ?? [])
  .filter((item) => item.taxonomy_key === 'ths_concept_flow' && item.mapped_members > 0)
  .sort((left, right) => Number(right.net_inflow ?? -Infinity) - Number(left.net_inflow ?? -Infinity)));
const selectedReviewBoard = computed(() => reviewConceptBoards.value.find((item) => item.sector_key === selectedReviewBoardKey.value) ?? reviewConceptBoards.value[0] ?? null);
const selectedReviewBoardStocks = computed(() => selectedReviewBoard.value?.top_stocks ?? []);
const completedBackfillBoards = computed(() => conceptBackfill.value.states.filter((item) => item.state === 'completed' || item.state === 'empty').reduce((total, item) => total + Number(item.boards || 0), 0));
const closeIndexRegime = computed(() => closeStrategyReview.value?.report?.index_breadth_context?.multi_index_regime ?? null);
const closeShortTermReview = computed(() => closeStrategyReview.value?.report?.short_term_review ?? null);
const indexRegimeLabel = computed(() => ({
  corrective_rebound: '纠错反弹情景', trend_recovery: '趋势修复', weak_or_declining: '弱势/下行', mixed_transition: '混合过渡', insufficient_index_history: '历史不足',
}[closeIndexRegime.value?.state ?? ''] ?? closeIndexRegime.value?.state ?? '待生成'));
const indexRegimeType = computed((): 'success' | 'warning' | 'danger' | 'info' => closeIndexRegime.value?.state === 'trend_recovery' ? 'success' : closeIndexRegime.value?.state === 'corrective_rebound' || closeIndexRegime.value?.state === 'mixed_transition' ? 'warning' : closeIndexRegime.value?.state === 'weak_or_declining' ? 'danger' : 'info');
const indexLabel = (symbol: string) => ({ '000001.SH': '上证指数', '000300.SH': '沪深300', '399001.SZ': '深证成指', '399006.SZ': '创业板指' }[symbol] ?? symbol);
const equityChartOption = computed(() => ({
  tooltip: { trigger: 'axis' }, grid: { left: 48, right: 18, top: 24, bottom: 40 }, xAxis: { type: 'category', data: latestExperiment.value?.equity_curve.map((item) => item.date) ?? [] }, yAxis: { type: 'value', name: '净值', scale: true }, series: [{ type: 'line', smooth: true, showSymbol: false, data: latestExperiment.value?.equity_curve.map((item) => item.equity) ?? [], lineStyle: { width: 2, color: '#00897b' }, areaStyle: { color: 'rgba(0,137,123,0.12)' } }],
}));
const analystStockTimelineChartOption = computed(() => {
  const timeline = analystStockTimeline.value;
  const bars = timeline?.bars ?? [];
  const indexByTime = new Map(bars.map((bar, index) => [bar.bar_time, index]));
  const markerData = (timeline?.actions ?? []).filter((action) => action.mapping_status === 'mapped' && action.nearest_bar_time && indexByTime.has(action.nearest_bar_time)).map((action) => {
    const index = indexByTime.get(action.nearest_bar_time as string) ?? 0;
    const color = ['buy', 'watch', 'add_t', 'hold'].includes(action.action) ? '#d32f2f' : ['sell', 'reduce', 'avoid'].includes(action.action) ? '#1565c0' : '#f9a825';
    return { value: [index, action.nearest_bar_close ?? 0], name: `${action.analyst_id} · ${action.action}`, action, itemStyle: { color }, label: { show: true, formatter: action.action, color, fontSize: 10, position: 'top' } };
  });
  return { animation: false, tooltip: { trigger: 'axis' }, grid: { left: 52, right: 18, top: 34, bottom: 46 }, xAxis: { type: 'category', data: bars.map((bar) => chinaMinute(bar.bar_time)), boundaryGap: true }, yAxis: { type: 'value', scale: true }, dataZoom: [{ type: 'inside', filterMode: 'none' }], series: [
    { name: '分钟K线', type: 'candlestick', data: bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]), itemStyle: { color: '#d32f2f', color0: '#1565c0', borderColor: '#d32f2f', borderColor0: '#1565c0' } },
    { name: '分析师动作', type: 'scatter', data: markerData, symbolSize: 12, z: 10, tooltip: { formatter: (params: { data?: { action?: { analyst_id?: string; action?: string; event_time?: string; evidence?: string } } }) => { const action = params.data?.action; return `${action?.analyst_id ?? ''} · ${action?.action ?? ''}<br/>${action?.event_time ? chinaDateTime(action.event_time) : ''}<br/>${action?.evidence ?? ''}`; } } },
  ] };
});
const analystReviewChartOption = computed(() => {
  const points = analystWeeklyReview.value?.summary?.daily_points ?? analystDailyReview.value?.summary?.daily_points ?? [];
  return { animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 52, right: 18, top: 32, bottom: 42 }, xAxis: { type: 'category', data: points.map((item) => item.exchange_date) }, yAxis: [{ type: 'value', name: '观点净方向' }, { type: 'value', name: '市场涨跌%', axisLabel: { formatter: '{value}%' } }], series: [{ name: '观点净方向', type: 'bar', data: points.map((item) => item.net_direction_score ?? 0), itemStyle: { color: '#7e57c2' } }, { name: '市场均涨跌%', type: 'line', yAxisIndex: 1, smooth: true, data: points.map((item) => item.market_mean_change_pct ?? null), lineStyle: { color: '#00897b', width: 2 } }] };
});
const marketFlowLatest = computed(() => marketFlow.value.latest ?? marketFlow.value.items.at(-1) ?? null);
const marketFlowSectorHighlights = computed(() => [...(marketFlow.value.sector_daily ?? [])]
  .sort((left, right) => {
    const priority = (row: SectorFlowDailyFeature) => ['reversal_in', 'reversal_out', 'acceleration_in', 'acceleration_out'].includes(row.transition) ? 1 : 0;
    return priority(right) - priority(left)
      || Math.abs(Number(right.net_change_amount ?? 0)) - Math.abs(Number(left.net_change_amount ?? 0))
      || Number(right.lhb_stock_count ?? 0) - Number(left.lhb_stock_count ?? 0);
  }).slice(0, 20));
const marketFlowStateLabel = (value?: string | null) => ({
  flow_expansion: '资金扩张', flow_risk_off: '资金退潮', late_repair: '尾盘修复',
  flow_acceleration: '流入加速', flow_deterioration: '流入恶化', mixed_rotation: '轮动混合',
  risk_expansion: '放量扩张', distribution: '放量派发', weak_repair: '缩量修复',
  passive_decline: '缩量走弱', neutral_rotation: '中性轮动', insufficient: '数据不足',
}[value ?? ''] ?? value ?? '等待特征');
const marketFlowStateType = (value?: string | null): 'success' | 'warning' | 'danger' | 'info' => (
  ['flow_expansion', 'flow_acceleration', 'risk_expansion'].includes(value ?? '') ? 'success'
    : ['flow_risk_off', 'flow_deterioration', 'distribution', 'passive_decline'].includes(value ?? '') ? 'danger'
      : ['late_repair', 'weak_repair', 'mixed_rotation', 'neutral_rotation'].includes(value ?? '') ? 'warning' : 'info'
);
const sectorFlowTransitionLabel = (value?: string | null) => ({ reversal_in: '流出转流入', reversal_out: '流入转流出', acceleration_in: '流入加速', acceleration_out: '流出加速', persistent_in: '持续流入', persistent_out: '持续流出', flat: '平稳', insufficient: '证据不足' }[value ?? ''] ?? value ?? '-');
const sectorFlowTransitionType = (value?: string | null): 'success' | 'warning' | 'danger' | 'info' => value === 'reversal_in' || value === 'acceleration_in' ? 'success' : value === 'reversal_out' || value === 'acceleration_out' ? 'danger' : value === 'persistent_out' ? 'warning' : 'info';
const marketFlowChartOption = computed(() => {
  const rows = marketFlow.value.items.filter((item) => item.cadence === 'minute');
  return {
    animation: false,
    tooltip: { trigger: 'axis' },
    grid: { left: 58, right: 54, top: 34, bottom: 46 },
    xAxis: { type: 'category', data: rows.map((item) => chinaMinute(item.observed_at)), boundaryGap: false },
    yAxis: [
      { type: 'value', name: '流入广度%', min: 0, max: 100 },
      { type: 'value', name: '资金中位数', scale: true },
    ],
    dataZoom: [{ type: 'inside', filterMode: 'none' }],
    series: [
      { name: '概念流入广度', type: 'line', showSymbol: false, smooth: true, data: rows.map((item) => item.concept_positive_ratio == null ? null : Number(item.concept_positive_ratio) * 100), lineStyle: { color: '#b71c1c', width: 2 }, areaStyle: { color: 'rgba(183,28,28,0.08)' } },
      { name: '概念资金中位数', type: 'line', yAxisIndex: 1, showSymbol: false, data: rows.map((item) => item.concept_median_flow ?? null), lineStyle: { color: '#1565c0', width: 1.5 } },
    ],
  };
});
const boardFlowSeriesRows = computed(() => Object.values(boardFlowSeries.value).sort((left, right) => left.label.localeCompare(right.label, 'zh-CN')));
const boardFlowLatestSnapshot = computed(() => boardFlowSnapshots.value.at(-1) ?? null);
const boardFlowLatestValues = computed(() => {
  const latest = boardFlowLatestSnapshot.value?.observed_at;
  return boardFlowSeriesRows.value.flatMap((item) => {
    const point = item.points.at(-1);
    return point && point.observed_at === latest ? [{
      key: `${item.taxonomy_key}:${item.sector_key}`, label: item.label, value: point.net_inflow,
    }] : [];
  }).sort((left, right) => right.value - left.value);
});
const boardFlowHighlighted = computed(() => new Set([
  ...boardFlowLatestValues.value.slice(0, 10).map((item) => item.key),
  ...boardFlowLatestValues.value.slice(-10).map((item) => item.key),
]));
const boardFlowWindowText = computed(() => boardFlowDisplaySlots.value.length
  ? `${chinaMinute(boardFlowDisplaySlots.value[0])}–${chinaMinute(boardFlowDisplaySlots.value.at(-1))}（上交所）`
  : '等待上交所观察时段');
const boardFlowGaps = computed(() => {
  const observed = new Set(boardFlowSnapshots.value.map((item) => new Date(item.observed_at).getTime()));
  let gaps = 0; let insideGap = false;
  for (const slot of boardFlowDisplaySlots.value) {
    const missing = !observed.has(new Date(slot).getTime());
    if (missing && !insideGap) gaps += 1;
    insideGap = missing;
  }
  return gaps;
});
const boardRotationKind = (item: BoardRotationEvent) => item.event_type === 'cross_zero'
  ? (item.direction === 'inflow' ? '流出转流入' : '流入转流出')
  : (item.direction === 'inflow' ? '流入加速' : '流出加速');
const boardRotationStateType = (value: BoardRotationEvent['state']): 'success' | 'warning' | 'danger' | 'info' => value === 'alerted' ? 'success' : value === 'confirmed' || value === 'confirming' ? 'warning' : value === 'expired' ? 'info' : 'danger';
const boardRotationStateText = (value: BoardRotationEvent['state']) => ({ confirming: '待下一分钟确认', confirmed: '已确认', alerted: '已记录', expired: '方向未延续' }[value] ?? value);
const boardRotationDeliveryText = (_item: BoardRotationEvent) => '仅前端证据';
const boardFlowChartOption = computed(() => {
  const focus = new Set(boardFlowFocus.value);
  const slots = boardFlowDisplaySlots.value;
  const labels = slots.map((slot) => chinaMinute(slot));
  const lines = boardFlowSeriesRows.value.map((item, index) => {
    const key = `${item.taxonomy_key}:${item.sector_key}`;
    const highlighted = boardFlowHighlighted.value.has(key);
    const latest = item.points.at(-1)?.net_inflow ?? 0;
    const ordered = [...item.points].sort((left, right) => left.observed_at.localeCompare(right.observed_at));
    const realByMinute = new Map(ordered.map((point) => [new Date(point.observed_at).getTime(), point]));
    const firstReal = ordered[0]; let previousReal: BoardFlowPoint | undefined;
    const data = slots.map((slot) => {
      const real = realByMinute.get(new Date(slot).getTime());
      if (real) previousReal = real;
      const source = real ?? previousReal ?? firstReal;
      if (!source) return { value: null, imputed: false, sourceObservedAt: null };
      return {
        value: source.net_inflow, imputed: !real, sourceObservedAt: source.observed_at,
        imputation: real ? null : previousReal ? 'forward_fill' : 'nearest_next',
      };
    });
    const focused = focus.size === 0 || focus.has(key);
    const hue = Math.round((index * 137.508) % 360);
    const color = highlighted ? (latest >= 0 ? '#c62828' : '#16833b') : `hsl(${hue}, 58%, 43%)`;
    return {
      name: item.label, type: 'line', data, showSymbol: false, connectNulls: true,
      animation: false, sampling: 'lttb', emphasis: { focus: 'series', lineStyle: { width: 3, opacity: 1 } },
      lineStyle: { color, width: highlighted ? 2.1 : 0.8, opacity: focused ? (highlighted ? 0.92 : 0.2) : 0.025 },
      itemStyle: { color },
    };
  });
  return {
    animation: false,
    tooltip: {
      trigger: 'item', confine: true,
      formatter: (params: { seriesName?: string; name?: string; data?: { value?: number | null; imputed?: boolean; sourceObservedAt?: string | null } }) => {
        const point = params.data; if (!point || point.value === null || point.value === undefined) return params.seriesName ?? '';
        const fill = point.imputed ? `<br/><span style="color:#b26a00">补点：沿用 ${chinaMinute(point.sourceObservedAt)} 真实值</span>` : '<br/>真实采样';
        return `${params.seriesName ?? ''}<br/>${params.name ?? ''}（上交所）<br/>净流入 ${Number(point.value).toFixed(2)} 亿元${fill}`;
      },
    },
    grid: { left: 62, right: 24, top: 28, bottom: 64 },
    xAxis: { type: 'category', data: labels, boundaryGap: false, name: '上交所时间', axisLabel: { hideOverlap: true }, splitLine: { show: false } },
    yAxis: { type: 'value', name: '净流入（亿元）', axisLine: { show: true, onZero: true }, splitLine: { lineStyle: { color: '#edf0f5' } } },
    dataZoom: [{ type: 'inside', filterMode: 'none' }, { type: 'slider', height: 22, bottom: 14, filterMode: 'none' }],
    series: lines,
  };
});

async function loadConfig() { const data = await getJson<{ routes?: Route[] }>('/api/config'); routes.value = data.routes ?? []; relayTag.value ||= routes.value[0]?.tag ?? ''; }
async function loadBoardFlowCurves(reset = false) {
  if (boardFlowLoading.value) return;
  if (reset) { boardFlowSeries.value = {}; boardFlowSnapshots.value = []; boardFlowCursor.value = null; boardFlowFocus.value = []; }
  boardFlowLoading.value = true; boardFlowError.value = '';
  try {
    const params = new URLSearchParams({ taxonomy: boardFlowTaxonomy.value });
    if (boardFlowDate.value) params.set('trade_date', boardFlowDate.value);
    if (!reset && boardFlowCursor.value) params.set('since', boardFlowCursor.value);
    const result = await getJson<BoardFlowResponse>(`/api/research/market/sectors/intraday/curves?${params.toString()}`);
    const merged = { ...boardFlowSeries.value };
    for (const incoming of result.items ?? []) {
      const key = `${incoming.taxonomy_key}:${incoming.sector_key}`;
      const current = merged[key] ?? { ...incoming, points: [] };
      const points = new Map(current.points.map((point) => [point.observed_at, point]));
      for (const point of incoming.points ?? []) points.set(point.observed_at, point);
      merged[key] = { ...incoming, points: [...points.values()].sort((left, right) => left.observed_at.localeCompare(right.observed_at)) };
    }
    const snapshots = new Map(boardFlowSnapshots.value.map((item) => [item.observed_at, item]));
    for (const item of result.snapshots ?? []) snapshots.set(item.observed_at, item);
    boardFlowSeries.value = merged;
    boardFlowSnapshots.value = [...snapshots.values()].sort((left, right) => left.observed_at.localeCompare(right.observed_at));
    boardFlowDate.value = result.trade_date;
    boardFlowDisplaySlots.value = result.display_slots ?? [];
    boardFlowIsExchangeToday.value = Boolean(result.is_exchange_today);
    boardFlowCursor.value = result.cursor ?? boardFlowCursor.value;
    boardFlowNotice.value = result.notice ?? '';
  } catch (error) {
    boardFlowError.value = error instanceof Error ? error.message : String(error);
  } finally { boardFlowLoading.value = false; }
}
async function loadMarketFlowFeatures() {
  marketFlowError.value = '';
  try {
    const params = new URLSearchParams();
    if (boardFlowDate.value) params.set('trade_date', boardFlowDate.value);
    const result = await getJson<MarketFlowResponse>(`/api/research/market/flow/features?${params.toString()}`);
    marketFlow.value = result;
    if (!boardFlowDate.value) boardFlowDate.value = result.trade_date;
  } catch (error) {
    marketFlowError.value = error instanceof Error ? error.message : String(error);
  }
}
async function loadBoardRotationEvents() {
  try {
    const result = await getJson<{ items?: BoardRotationEvent[] }>('/api/research/intraday/board-rotations/latest?limit=20');
    boardRotationEvents.value = result.items ?? [];
  } catch {
    // Curves remain usable if the optional local rotation-evidence card is unavailable.
  }
}
async function loadBoardStockMining() {
  try { boardStockMining.value = await getJson<BoardStockMining>('/api/research/intraday/board-stock-mining/latest?limit=12'); } catch {
    // The rest of the board dashboard remains usable before the migration lands.
  }
}
async function loadLimitLinkageMining() {
  try { limitLinkageMining.value = await getJson<LimitLinkageMining>('/api/research/intraday/limit-linkage/latest?limit=20'); } catch {
    // The rest of the board dashboard remains usable before the migration lands.
  }
}
function resetBoardFlowCurves() { void loadBoardFlowCurves(true); void loadMarketFlowFeatures(); }
async function loadRealtimeServices() {
  realtimeLoading.value = true; realtimeError.value = '';
  try {
    const [services, adapter, runtime] = await Promise.all([
      getJson<RealtimeServiceStatus>('/api/v1/intraday/services/status'),
      getJson<AdapterHealth>('/health'), getJson<typeof runtimeHealth.value>('/api/research/runtime/health'),
    ]);
    realtimeServices.value = services; adapterHealth.value = adapter; runtimeHealth.value = runtime;
    const feishu = realtimeServices.value.items?.find((item) => item.key === 'feishu_alert');
    if (feishu && (adapter.status !== 'ok' || !adapter.quant_alert_configured)) {
      feishu.state = adapter.status === 'ok' ? 'disabled' : 'degraded';
      feishu.last_error = adapter.status === 'ok' ? '飞书提醒目标或内部鉴权未配置' : '飞书适配器健康检查失败';
    }
  } catch (error) {
    realtimeError.value = error instanceof Error ? error.message : String(error);
  } finally { realtimeLoading.value = false; }
}
async function loadGroupRelayStatus() {
  groupRelayLoading.value = true; groupRelayError.value = '';
  try { groupRelayStatus.value = await getJson<GroupRelayStatus>('/api/group-relay/status'); }
  catch (error) { groupRelayError.value = error instanceof Error ? error.message : String(error); }
  finally { groupRelayLoading.value = false; }
}
async function loadFeishuWorkbench() {
  feishuWorkbenchLoading.value = true; feishuWorkbenchError.value = '';
  try {
    const [status, messages] = await Promise.all([
      getJson<FeishuWorkbenchStatus>('/api/feishu-workbench/status'),
      getJson<{ items?: FeishuWorkbenchMessage[] }>('/api/feishu-workbench/messages?limit=80'),
    ]);
    feishuWorkbench.value = status; feishuWorkbenchMessages.value = messages.items ?? [];
  } catch (error) { feishuWorkbenchError.value = error instanceof Error ? error.message : String(error); }
  finally { feishuWorkbenchLoading.value = false; }
}
async function inspectFeishuApplication() {
  feishuWorkbenchAction.value = 'application-inspection';
  try {
    const path = '/api/feishu-workbench/application-inspection';
    const result = await decodeJson<{ inspection?: FeishuApplicationInspection }>(await fetch(path, { method: 'POST', headers: { accept: 'application/json' } }), path);
    ElMessage.info(result.inspection?.message ?? '已完成飞书后台配置复核'); await loadFeishuWorkbench();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  finally { feishuWorkbenchAction.value = ''; }
}
function workbenchMessageText(item: FeishuWorkbenchMessage) {
  try {
    const content = item.message?.body?.content ? JSON.parse(item.message.body.content) : {};
    return String(content.text ?? content.title ?? `[${item.message?.msg_type ?? '消息'}]`).slice(0, 160);
  } catch { return `[${item.message?.msg_type ?? '消息'}]`; }
}
const workbenchWorkflowText = (state?: string) => ({ new: '待处理', research: '纳入研究', focus: '重点关注', task: '已建任务', ignored: '已忽略', recalled: '已撤回' }[state ?? 'new'] ?? state ?? '待处理');
const oauthAuditLabel = (audit?: OAuthScopeAudit | null) => audit?.verified === true ? '权限已验证' : audit?.verified === false ? `缺少 ${audit.missing_scopes?.length ?? 0} 项权限` : '权限待验证';
const oauthAuditTagType = (audit?: OAuthScopeAudit | null) => audit?.verified === true ? 'success' : audit?.verified === false ? 'danger' : 'warning';
const relayDeliveryLabel = (state?: GroupRelaySourceStatus['delivery_state']) => ({ verified: '已实际转发', awaiting_message: '等待新消息验证', failed: '转发失败' }[state ?? 'awaiting_message'] ?? '等待新消息验证');
const relayDeliveryTagType = (state?: GroupRelaySourceStatus['delivery_state']) => state === 'verified' ? 'success' : state === 'failed' ? 'danger' : 'warning';
const ingestionDeliveryLabel = (ingestion?: IngestionRouteStatus | null) => {
	if (!ingestion?.latest_status) return '暂无汇总群入站';
	if (ingestion.state === 'filtered') return '系统消息已过滤';
	if (ingestion.state === 'completed') return ingestion.remote_batch_id ? '远端已入档' : '已提交远端';
	if (ingestion.state === 'stalled') return '下游状态卡住';
	if (ingestion.state === 'failed') return '远端待恢复';
	return 'n8n / 远端处理中';
};
const ingestionDeliveryTagType = (ingestion?: IngestionRouteStatus | null): 'success' | 'warning' | 'danger' | 'info' => ingestion?.state === 'completed' ? 'success' : ingestion?.state === 'stalled' || ingestion?.state === 'failed' ? 'danger' : ingestion?.state === 'processing' ? 'warning' : 'info';
const applicationInspectionLabel = (inspection?: FeishuApplicationInspection) => ({ verified: '已读取', missing_inspection_scope: '缺少复核权限', error: '复核失败', not_checked: '未复核' }[inspection?.status ?? 'not_checked'] ?? '未复核');
const applicationInspectionTagType = (inspection?: FeishuApplicationInspection) => inspection?.status === 'verified' ? 'success' : inspection?.status === 'missing_inspection_scope' || inspection?.status === 'error' ? 'danger' : 'warning';
const targetChatInspectionLabel = (inspection?: FeishuApplicationInspection) => ({ verified: '应用可读取', error: '读取失败', not_configured: '未配置', not_checked: '未检查' }[inspection?.target_chat?.status ?? 'not_checked'] ?? '未检查');
const targetChatInspectionTagType = (inspection?: FeishuApplicationInspection) => inspection?.target_chat?.status === 'verified' ? 'success' : inspection?.target_chat?.status === 'error' ? 'danger' : 'warning';
const capabilityAuthorizationLabel = (item: FeishuCapability) => ({ verified: '用户权限已验证', missing: '用户权限缺失', unknown: '用户权限待验证', awaiting_verification: '待后台验收', not_required: '无需权限验收' }[item.authorization_status ?? 'awaiting_verification'] ?? '待后台验收');
const capabilityAuthorizationTagType = (item: FeishuCapability) => item.authorization_status === 'verified' || item.authorization_status === 'not_required' ? 'success' : item.authorization_status === 'missing' ? 'danger' : 'warning';
async function runWorkbenchAction(item: FeishuWorkbenchMessage, action: string) {
  feishuWorkbenchAction.value = `${item.source_message_id}:${action}`;
  try {
    const path = '/api/feishu-workbench/actions';
    await decodeJson(await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify({ source_message_id: item.source_message_id, action }) }), path);
    ElMessage.success('协作状态已更新'); await loadFeishuWorkbench();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  finally { feishuWorkbenchAction.value = ''; }
}
async function searchFeishuMessages() {
  if (!workbenchSearch.value.trim()) return;
  feishuWorkbenchAction.value = 'search';
  try {
    const path = '/api/feishu-workbench/message-search';
    workbenchSearchResult.value = await decodeJson(await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify({ query: workbenchSearch.value.trim() }) }), path);
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  finally { feishuWorkbenchAction.value = ''; }
}
function openWorkbenchIntegration(kind: 'documents' | 'wiki-documents' | 'base-records' | 'calendar-events' | 'approvals') {
  const presets = {
    documents: { title: '新建飞书文档', payloadText: '{\n  "title": "群消息研究笔记",\n  "contentType": "markdown",\n  "content": "# 群消息研究笔记\\n\\n请在此补充结论。"\n}' },
    'wiki-documents': { title: '新建并归档到 Wiki', payloadText: '{\n  "title": "群消息研究笔记",\n  "contentType": "markdown",\n  "content": "# 群消息研究笔记\\n\\n请在此补充结论。"\n}' },
    'base-records': { title: '写入 Base 研究台账', payloadText: '{\n  "fields": {\n    "标题": "群消息跟进",\n    "状态": "待处理"\n  }\n}' },
    'calendar-events': { title: '创建日历提醒', payloadText: `{\n  "summary": "群消息跟进",\n  "description": "",\n  "startTime": "${new Date(Date.now() + 3600_000).toISOString()}",\n  "endTime": "${new Date(Date.now() + 7200_000).toISOString()}"\n}` },
    approvals: { title: '发起审批', payloadText: '{\n  "form": {}\n}' },
  };
  workbenchIntegration.value = { kind, ...presets[kind] }; workbenchIntegrationDialog.value = true;
}
async function runWorkbenchEndpoint(path: string, key: string, body: Record<string, unknown> = {}) {
  feishuWorkbenchAction.value = key;
  try {
    await decodeJson(await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(body) }), path);
    ElMessage.success('已提交到飞书'); await loadFeishuWorkbench();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  finally { feishuWorkbenchAction.value = ''; }
}
async function createWorkbenchDigest() {
  try { await ElMessageBox.confirm('将把近期未忽略的汇总消息合并为一条摘要发送到汇总群。', '生成群摘要', { type: 'warning', confirmButtonText: '生成并发送', cancelButtonText: '取消' }); }
  catch { return; }
  await runWorkbenchEndpoint('/api/feishu-workbench/digests', 'digest', { limit: 12 });
}
async function createWorkbenchTab() {
  try { await ElMessageBox.confirm('将为汇总群创建一个指向公网 H5 工作台的群 Tab。', '创建汇总群 Tab', { type: 'warning', confirmButtonText: '创建', cancelButtonText: '取消' }); }
  catch { return; }
  await runWorkbenchEndpoint('/api/feishu-workbench/group-tabs', 'group-tab');
}
async function submitWorkbenchIntegration() {
  let payload: Record<string, unknown>;
  try { payload = JSON.parse(workbenchIntegration.value.payloadText) as Record<string, unknown>; } catch { ElMessage.error('请输入有效 JSON'); return; }
  const path = `/api/feishu-workbench/${workbenchIntegration.value.kind}`;
  feishuWorkbenchAction.value = `integration:${workbenchIntegration.value.kind}`;
  try {
    await decodeJson(await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(payload) }), path);
    ElMessage.success('已提交到飞书'); workbenchIntegrationDialog.value = false;
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  finally { feishuWorkbenchAction.value = ''; }
}
function openCreateGroupRelayRoute() {
  groupRelayRouteForm.value = { key: '', chat_name: '', chat_id: '', tag: '', target_chat_ids_text: '', target_chat_names_text: '', enabled: true };
  groupRelayRouteDialog.value = true;
}
function openEditGroupRelayRoute(route: GroupRelaySourceStatus) {
  groupRelayRouteForm.value = { key: route.key, chat_name: route.chat_name, chat_id: '', tag: route.tag, target_chat_ids_text: (route.target_chat_ids ?? []).join(', '), target_chat_names_text: '', enabled: route.enabled !== false };
  groupRelayRouteDialog.value = true;
}
async function saveGroupRelayRoute() {
  const form = groupRelayRouteForm.value;
  groupRelayRouteSaving.value = true;
  try {
    const path = form.key ? `/api/group-relay/routes/${encodeURIComponent(form.key)}` : '/api/group-relay/routes';
    const response = await fetch(path, {
      method: form.key ? 'PUT' : 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ chat_name: form.chat_name, chat_id: form.chat_id || undefined, tag: form.tag, target_chat_ids: form.target_chat_ids_text.split(',').map((value) => value.trim()).filter(Boolean), target_chat_names: form.target_chat_names_text.split(',').map((value) => value.trim()).filter(Boolean), enabled: form.enabled }),
    });
    await decodeJson<{ route: GroupRelaySourceStatus }>(response, path);
    groupRelayRouteDialog.value = false;
    ElMessage.success(form.key ? '源群配置已更新，将在下一次轮询生效' : '源群已注册，将在下一次轮询建立基线');
    await loadGroupRelayStatus();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  finally { groupRelayRouteSaving.value = false; }
}
async function setGroupRelayRouteEnabled(route: GroupRelaySourceStatus, enabled: boolean) {
  const path = `/api/group-relay/routes/${encodeURIComponent(route.key)}`;
  try {
    await decodeJson(await fetch(path, { method: 'PUT', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify({ chat_name: route.chat_name, tag: route.tag, enabled }) }), path);
    ElMessage.success(enabled ? '已启用，下一次轮询生效' : '已停用该源群');
    await loadGroupRelayStatus();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
}
async function deleteGroupRelayRoute(route: GroupRelaySourceStatus) {
  try {
    await ElMessageBox.confirm(`确认停止监听“${route.chat_name}”并删除其注册信息？已有转发记录会保留。`, '删除源群', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' });
    const path = `/api/group-relay/routes/${encodeURIComponent(route.key)}`;
    const response = await fetch(path, { method: 'DELETE', headers: { accept: 'application/json' } });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})) as { message?: string };
      throw new Error(payload.message ?? `HTTP ${response.status}`);
    }
    ElMessage.success('源群已删除，下一次轮询起停止监听');
    await loadGroupRelayStatus();
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : String(error));
  }
}
async function loadResearch() {
  loading.value = true; researchError.value = '';
  try {
    const [overviewResult, researchResult] = await Promise.allSettled([
      getJson<ResearchOverview>('/api/research/overview'),
      Promise.all([
        getJson<{ items?: RemoteReport[] }>('/api/research/reports?limit=30'), getJson<{ items?: AnalystClaim[] }>('/api/research/claims?limit=80'), getJson<{ items?: ProviderHealth[] }>('/api/research/providers'), getJson<{ items?: ProviderApiCapability[] }>('/api/research/provider-capabilities'), getJson<typeof catalog.value>('/api/research/tushare/catalog'), getJson<{ items?: MarketSnapshot[] }>('/api/research/market/snapshots?limit=20'), getJson<{ items?: Sector[] }>('/api/research/market/sectors?taxonomy_key=ths_index_n&limit=500'), getJson<{ items?: SectorFlow[] }>('/api/research/market/sector-flows?taxonomy_key=ths_industry&limit=100'), getJson<{ items?: ConceptSignal[] }>('/api/research/market/sectors/concepts?limit=100'), getJson<{ items?: ConceptCandidate[] }>('/api/research/market/sectors/concepts/candidates?limit=100'), getJson<{ items?: Announcement[] }>('/api/research/events/announcements?limit=100'), getJson<{ items?: Announcement[] }>('/api/research/events/lhb?limit=100'), getJson<{ report?: BoardReviewReport | null }>('/api/research/market/sectors/review/report/latest'), getJson<ConceptBackfill>('/api/research/market/sectors/concepts/members/backfill/status'), getJson<{ review?: StrategyReview | null }>('/api/research/strategy/reviews/latest?session=close'), getJson<{ run?: PostCloseStrategyRun | null; candidates?: PostCloseCandidate[] }>('/api/research/strategy/post-close/latest'), getJson<{ run?: StrategyPatternRun | null; limit_pool?: LimitPoolRow[]; limit_ladder?: LimitLadderRow[]; continuation_candidates?: LimitPoolRow[]; dragon_leader_candidates?: LimitPoolRow[]; dragon_leader_market_context?: DragonLeaderWatch['market_context']; pool_coverage?: LimitPoolCoverage; picks?: StrategyPatternSample[]; samples?: StrategyPatternSample[] }>('/api/research/strategy/pattern-mining/latest'), getJson<{ recommendations?: Recommendation[] }>('/api/research/recommendations'), getJson<{ items?: UniverseMember[] }>('/api/research/universes/core'), getJson<{ items?: FeatureItem[] }>('/api/research/features/latest?universe_key=core'), getJson<{ items?: ClaimReview[] }>('/api/research/claim-review?status=pending'), getJson<{ items?: Factor[] }>('/api/research/factors'), getJson<{ items?: FactorEvaluation[] }>('/api/research/factor-evaluations?universe_key=all_a'), getJson<{ items?: Strategy[] }>('/api/research/strategies'), getJson<{ items?: StrategyExperiment[] }>('/api/research/strategy-experiments?universe_key=all_a'), getJson<{ items?: StrategyExperiment[] }>('/api/research/strategy-experiments-watchlist?universe_key=watchlist&limit=10'), getJson<{ items?: Framework[] }>('/api/research/frameworks'), getJson<TrainingRoadmap>('/api/research/training/roadmap'), getJson<{ items?: QualityIssue[] }>('/api/research/quality?limit=100'), getJson<{ items?: MinuteImport[]; offline_directory?: string }>('/api/research/minute/imports'),
      ]),
    ]);
    if (overviewResult.status === 'fulfilled') overview.value = overviewResult.value;
    else researchError.value = `研究概览读取失败：${overviewResult.reason instanceof Error ? overviewResult.reason.message : String(overviewResult.reason)}`;
    if (researchResult.status !== 'fulfilled') throw researchResult.reason;
    const [reportsData, claimsData, healthData, capabilityData, catalogData, snapshotData, sectorData, sectorFlowData, conceptSignalData, conceptCandidateData, announcementData, lhbData, boardReviewData, backfillData, strategyReviewData, postCloseStrategyData, patternData, recommendationData, universeData, featuresData, reviewsData, factorData, factorEvaluationData, strategyData, experimentData, mainWaveData, frameworkData, roadmapData, qualityData, minuteData] = researchResult.value;
    reports.value = reportsData.items ?? []; claims.value = claimsData.items ?? []; providerHealth.value = healthData.items ?? []; providerApiCapabilities.value = capabilityData.items ?? []; catalog.value = catalogData; marketSnapshots.value = snapshotData.items ?? []; sectors.value = sectorData.items ?? []; sectorFlows.value = sectorFlowData.items ?? []; conceptSignals.value = conceptSignalData.items ?? []; conceptCandidates.value = conceptCandidateData.items ?? []; announcements.value = announcementData.items ?? []; lhbEvents.value = lhbData.items ?? []; closeBoardReport.value = boardReviewData.report ?? null; conceptBackfill.value = backfillData; closeStrategyReview.value = strategyReviewData.review ?? null; postCloseStrategyRun.value = postCloseStrategyData.run ?? null; postCloseCandidates.value = postCloseStrategyData.candidates ?? []; strategyPatternRun.value = patternData.run ?? null; strategyLimitPool.value = patternData.limit_pool ?? []; strategyLimitLadder.value = patternData.limit_ladder ?? []; strategyContinuationCandidates.value = patternData.continuation_candidates ?? []; strategyDragonLeaderCandidates.value = patternData.dragon_leader_candidates ?? []; strategyDragonLeaderMarket.value = patternData.dragon_leader_market_context ?? {}; strategyPoolCoverage.value = patternData.pool_coverage ?? {}; strategyPatternPicks.value = patternData.picks ?? []; strategyPatternSamples.value = patternData.samples ?? []; recommendations.value = recommendationData.recommendations ?? []; universe.value = universeData.items ?? []; featureItems.value = featuresData.items ?? []; claimReviews.value = reviewsData.items ?? []; factors.value = factorData.items ?? []; factorEvaluations.value = factorEvaluationData.items ?? []; strategies.value = strategyData.items ?? []; strategyExperiments.value = experimentData.items ?? []; mainWaveExperiments.value = mainWaveData.items ?? []; frameworks.value = frameworkData.items ?? []; trainingRoadmap.value = roadmapData; qualityIssues.value = qualityData.items ?? []; minuteImports.value = minuteData.items ?? []; minuteDirectory.value = minuteData.offline_directory ?? '';
    const [outcomeData, scorecardData, messageData, skillData, analystResearchData, paperData, funnelData, observationData, governanceData, syncHealthData, evaluationData, dailyReviewData, weeklyReviewData, analystReviewRunData, automationRunData, promptLabData, ablationData, strategyHealthData] = await Promise.all([
      getJson<{ items?: IntradayOutcome[]; summary?: IntradayOutcomeSummary[]; attribution_summary?: IntradayAttributionSummary[]; attribution_validation_gate?: AttributionValidationGate }>('/api/research/intraday/outcomes/latest?limit=100'),
      getJson<{ items?: AnalystScorecard[]; readiness?: AnalystReadiness[] }>('/api/research/analyst-scorecards'),
      getJson<{ items?: RemoteMessage[] }>('/api/research/remote-archive/messages?limit=60'),
      getJson<{ items?: AnalystSkillProfile[] }>('/api/research/analyst-skills?limit=20'),
      getJson<AnalystResearchStatus>('/api/research/analyst-research/status'),
      getJson<PaperStatus>('/api/research/paper/status?limit=20'),
      getJson<StrategyFunnel>('/api/research/strategy/funnel?limit=30'),
      getJson<{ items?: AnalystObservation[] }>('/api/research/analyst-research/observations?limit=80'),
      getJson<StrategyGovernance>('/api/research/strategy/governance'),
      getJson<typeof analystSyncHealth.value>('/api/research/analyst-research/sync-health'),
      getJson<AnalystMarketEvaluation>('/api/research/analyst-research/market-evaluation'),
      getJson<{ review?: AnalystMarketReview | null }>('/api/research/analyst-research/reviews/latest?cadence=daily'),
      getJson<{ review?: AnalystMarketReview | null }>('/api/research/analyst-research/reviews/latest?cadence=weekly'),
      getJson<{ items?: AutomationRun[] }>('/api/research/automation/runs?task_key=analyst_market_review&limit=5'),
      getJson<{ items?: AutomationRun[] }>('/api/research/automation/runs?limit=30'),
      getJson<AnalystPromptLab>('/api/research/analyst-prompt-lab/status?limit=30'),
      getJson<StrategyAblation>('/api/research/strategy/ablation/latest?limit=30'),
      getJson<StrategyHealth>('/api/research/strategy/health'),
    ]);
    intradayOutcomes.value = outcomeData.items ?? []; intradayOutcomeSummary.value = outcomeData.summary ?? [];
    intradayAttributionSummary.value = outcomeData.attribution_summary ?? [];
    attributionValidationGate.value = outcomeData.attribution_validation_gate ?? attributionValidationGate.value;
    analystScorecards.value = scorecardData.items ?? []; analystReadiness.value = scorecardData.readiness ?? [];
    remoteMessages.value = messageData.items ?? []; analystSkills.value = skillData.items ?? []; analystResearchStatus.value = analystResearchData; paperStatus.value = paperData; strategyFunnel.value = funnelData; analystObservations.value = observationData.items ?? []; strategyGovernance.value = governanceData; analystSyncHealth.value = syncHealthData; analystMarketEvaluation.value = evaluationData; analystDailyReview.value = dailyReviewData.review ?? null; analystWeeklyReview.value = weeklyReviewData.review ?? null; analystReviewRuns.value = analystReviewRunData.items ?? []; automationRuns.value = automationRunData.items ?? []; analystPromptLab.value = promptLabData; strategyAblation.value = ablationData; strategyHealth.value = strategyHealthData;
    if (!universeText.value) universeText.value = universe.value.filter((item) => item.enabled).map((item) => item.symbol).join(', ');
    if (!sectorFlowDate.value) sectorFlowDate.value = sectorFlows.value[0]?.trading_date ?? overview.value.latest_market_snapshot?.exchange_date ?? '';
    if (!selectedFactors.value.length) selectedFactors.value = factors.value.filter((item) => item.implementation === 'native_sql').map((item) => item.factor_key);
  } catch (error) { researchError.value = error instanceof Error ? error.message : String(error); } finally { loading.value = false; }
}
async function runAction(label: string, path: string, body: Record<string, unknown> = {}, confirmation = true) {
  if (confirmation) await ElMessageBox.confirm(`确认执行${label}？`, '研究操作', { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' });
  actionLoading.value = label;
  try { const result = await postJson<Record<string, unknown>>(path, body); ElMessage.success(`${label}：${String(result.status ?? '已提交')}`); await loadResearch(); return result; } catch (error) { if (error !== 'cancel') ElMessage.error(`${label}失败：${error instanceof Error ? error.message : String(error)}`); return undefined; } finally { actionLoading.value = ''; }
}
async function runAnalystMarketReview(cadence: 'daily' | 'weekly') {
  analystReviewRunning.value = cadence;
  try {
    const result = await postJson<{ review?: AnalystMarketReview }>('/api/research/analyst-research/reviews/run', { cadence });
    if (cadence === 'daily') analystDailyReview.value = result.review ?? null; else analystWeeklyReview.value = result.review ?? null;
    ElMessage.success(`${cadence === 'daily' ? '日报' : '周报'}已生成`);
  } catch (error) { ElMessage.error(`分析师复盘失败：${error instanceof Error ? error.message : String(error)}`); }
  finally { analystReviewRunning.value = ''; }
}
async function loadAnalystStockTimeline() {
  const symbol = studySymbol.value.trim().toUpperCase();
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) { analystStockTimelineError.value = '请输入形如 603459.SH 的代码'; return; }
  analystStockTimelineLoading.value = true; analystStockTimelineError.value = '';
  try {
    const params = new URLSearchParams({ symbol, limit: '1500' });
    if (analystTimelineDate.value) { params.set('start_date', analystTimelineDate.value); params.set('end_date', analystTimelineDate.value); }
    if (analystTimelineAnalyst.value) params.set('analyst_id', analystTimelineAnalyst.value);
    analystStockTimeline.value = await getJson<AnalystStockTimeline>(`/api/research/analyst-research/stock-timeline?${params.toString()}`);
  } catch (error) { analystStockTimeline.value = null; analystStockTimelineError.value = error instanceof Error ? error.message : String(error); } finally { analystStockTimelineLoading.value = false; }
}
function openFetch(item?: CatalogItem) {
  if (item) {
    fetchForm.value.api_name = item.api_name;
    fetchForm.value.paramsText = JSON.stringify(item.sample_params ?? {}, null, 2);
    fetchForm.value.fields = '';
    fetchForm.value.max_rows = item.request_policy === 'market_hours_only' ? 10 : 100;
  }
  fetchDialogOpen.value = true;
}
function selectCatalog(rows: CatalogItem[]) { selectedCatalog.value = rows; }
async function refreshCatalog() {
  catalogRefreshing.value = true;
  try {
    catalog.value = await getJson<typeof catalog.value>('/api/research/tushare/catalog');
    selectedCatalog.value = [];
    ElMessage.success('能力状态已刷新');
  } catch (error) { ElMessage.error(`刷新能力状态失败：${error instanceof Error ? error.message : String(error)}`); } finally { catalogRefreshing.value = false; }
}
async function auditSelectedCatalog() {
  if (!selectedCatalog.value.length) { ElMessage.warning('请先选择需要核验的接口'); return; }
  if (selectedCatalog.value.length > 12) { ElMessage.error('单次最多核验 12 个接口'); return; }
  const symbol = /^\d{6}\.(SH|SZ|BJ)$/.test(studySymbol.value.trim().toUpperCase()) ? studySymbol.value.trim().toUpperCase() : '000636.SZ';
  actionLoading.value = '核验所选接口';
  try {
    const result = await postJson<{ status: string; results: CapabilityAuditRow[] }>('/api/research/tushare/audit', { api_names: selectedCatalog.value.map((item) => item.api_name), providers: ['primary', 'super_sdk', 'super_get'], symbol, max_rows: 10 });
    auditResults.value = result.results ?? [];
    ElMessage.success(`三条物理通道核验：${result.status}`);
    await loadResearch();
  } catch (error) { ElMessage.error(`接口核验失败：${error instanceof Error ? error.message : String(error)}`); } finally { actionLoading.value = ''; }
}
async function executeFetch() {
  let params: Record<string, unknown>;
  try { params = JSON.parse(fetchForm.value.paramsText); if (Array.isArray(params) || params === null) throw new Error('参数必须是 JSON 对象'); } catch (error) { ElMessage.error(`参数 JSON 无效：${error instanceof Error ? error.message : String(error)}`); return; }
  actionLoading.value = 'fetch';
  try {
    fetchResult.value = await postJson<Record<string, unknown>>('/api/research/tushare/fetch', { api_name: fetchForm.value.api_name, provider: fetchForm.value.provider, params, fields: fetchForm.value.fields || null, max_rows: fetchForm.value.max_rows });
    fetchDialogOpen.value = false; fetchResultOpen.value = true; ElMessage.success('原始证据已保存'); await loadResearch();
  } catch (error) { ElMessage.error(`取数失败：${error instanceof Error ? error.message : String(error)}`); } finally { actionLoading.value = ''; }
}
async function runStockStudy() {
  const symbol = studySymbol.value.trim().toUpperCase();
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) { ElMessage.error('代码格式应为 000636.SZ'); return; }
  studyLoading.value = true; studyError.value = ''; stockStudy.value = null;
  try {
    stockStudy.value = await postJson<StockStudy>(`/api/research/stocks/${symbol}/study`, { lookback_days: studyLookback.value });
    studySymbol.value = symbol; ElMessage.success(`${symbol} 的研究证据已刷新`); await loadResearch();
  } catch (error) { studyError.value = error instanceof Error ? error.message : String(error); } finally { studyLoading.value = false; }
}
async function probeRealtimeMinutes() {
  const symbol = studySymbol.value.trim().toUpperCase();
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) { ElMessage.error('代码格式应为 000636.SZ'); return; }
  const result = await runAction('验证双源实时接口', '/api/research/providers/realtime/probe', { symbols: [symbol], frequency: '1MIN' }, false);
  if (result?.results && Array.isArray(result.results)) auditResults.value = result.results as CapabilityAuditRow[];
}
async function probeAkshareSupplement() {
  const symbol = studySymbol.value.trim().toUpperCase();
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) { ElMessage.error('代码格式应为 000636.SZ'); return; }
  await runAction('AkShare补充探测', '/api/research/providers/akshare/probe', {
    symbol, lookback_days: studyLookback.value, include_supplements: true, include_macro_cross_asset: false, board_limit: 3,
  }, false);
}
async function probeAkshareMacroSupplement() {
  const symbol = studySymbol.value.trim().toUpperCase();
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) { ElMessage.error('代码格式应为 000636.SZ'); return; }
  await runAction('AkShare宏观跨资产补充', '/api/research/providers/akshare/probe', {
    symbol, lookback_days: studyLookback.value, include_supplements: true, include_macro_cross_asset: true,
    include_board_taxonomy: false, include_moneyflow: false, include_limit_pools: false, include_lhb_supplements: false,
    include_block_trades: false, include_corporate_risk: false, include_analyst_heat: false, include_index_fund: false, board_limit: 0,
  }, false);
}
async function saveUniverse() {
  const symbols = universeText.value.split(/[\s,;]+/).map((item) => item.trim().toUpperCase()).filter(Boolean);
  if (!symbols.length) { ElMessage.error('至少输入一个股票代码'); return; }
  await runAction('更新核心股票池', '/api/research/universes/members', { universe_key: 'core', symbols, enabled: true, priority: universePriority.value });
}
async function syncAllMarketUniverse() { await runAction('刷新全市场股票池', '/api/research/market/universe/sync', {}, true); }
async function runMarketSnapshot(session: 'midday' | 'close') { await runAction(session === 'midday' ? '生成午盘全市场快照' : '生成收盘全市场快照', '/api/research/market/snapshots/run', { session, universe_key: 'all_a', refresh_public_quotes: true }, true); }
async function syncFullMarketDaily() { await runAction('同步全市场收盘日线', '/api/research/market/full-daily/sync', {}, true); }
async function syncSectorDirectory() { await runAction('同步同花顺板块目录', '/api/research/market/sectors/sync', { all_types: true, sync_members: false }, true); }
async function syncSectorMembers() { await runAction('同步板块成分批次', '/api/research/market/sectors/sync', { index_type: 'N', sync_members: true, member_offset: sectorMemberOffset.value, member_limit: sectorMemberLimit.value }, true); }
async function syncSectorFlows() { if (!sectorFlowDate.value) { ElMessage.error('请选择交易日'); return; } await runAction('同步同花顺行业资金流', '/api/research/market/sector-flows/sync', { trade_date: sectorFlowDate.value, provider: 'super' }, true); }
async function syncConceptSignals() { if (!sectorFlowDate.value) { ElMessage.error('请选择交易日'); return; } await runAction('同步概念资金流与涨停强度', '/api/research/market/sectors/concepts/sync', { trade_date: sectorFlowDate.value, provider: 'super' }, true); }
async function syncConceptCandidates() { if (!sectorFlowDate.value) { ElMessage.error('请选择交易日'); return; } await runAction('生成概念涨停候选', '/api/research/market/sectors/concepts/candidates/sync', { trade_date: sectorFlowDate.value, provider: 'super', top_concepts: 8, leaders_per_concept: 3 }, true); }
async function runBoardResearch() { if (!sectorFlowDate.value) { ElMessage.error('请选择交易日'); return; } await runAction('板块到个股一键研究', '/api/research/market/sectors/concepts/research/run', { trade_date: sectorFlowDate.value, provider: 'super', top_concepts: 8, leaders_per_concept: 3, max_stock_studies: 6, study_lookback_days: 21, sync_announcements: true }, true); }
async function refreshCloseReview() { await runAction('保存收盘板块复盘', '/api/research/market/sectors/review/report/run', {}, true); }
async function runPostCloseStrategy() { await runAction('运行盘后蓄势与首动筛选', '/api/research/strategy/post-close/run', { limit: 20 }, true); }
async function runStrategyPatternMining() { await runAction('挖掘涨停拉升形态', '/api/research/strategy/pattern-mining/run', { max_symbols: 20, per_cohort: 6, refresh_limit_sources: true }, true); }
async function runPostCloseRefresh() {
  try {
    await ElMessageBox.confirm('确认执行盘后一键更新？', '研究操作', { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' });
    actionLoading.value = '盘后一键更新';
    postCloseRefresh.value = await postJson<PostCloseRefresh>(
      '/api/research/market/post-close/refresh', { include_macro_cross_asset: true, include_announcements: true },
    );
    ElMessage.success(`盘后一键更新：${postCloseRefresh.value.status ?? '已完成'}`);
    await loadResearch();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message.includes('already running')) {
      postCloseRefresh.value = { status: 'running', retry_hint: '已有盘后更新在运行；请等待其完成后刷新页面。' };
      ElMessage.info('已有盘后更新在运行中，本次未重复提交。');
      return;
    }
    if (error !== 'cancel') ElMessage.error(`盘后一键更新失败：${message}`);
  } finally {
    actionLoading.value = '';
  }
}
async function advanceConceptBackfill() { if (!sectorFlowDate.value) { ElMessage.error('请选择交易日'); return; } await runAction('补齐一批概念成员', '/api/research/market/sectors/concepts/members/backfill/run', { trade_date: sectorFlowDate.value, provider: 'super', batch_size: 25 }, true); }
async function settleIntradayOutcomes() { await runAction('结算盘中信号', '/api/research/intraday/outcomes/recompute', { as_of_date: sectorFlowDate.value || undefined }, true); }
async function recomputeAnalystScorecards() { await runAction('刷新分析师成绩单', '/api/research/scorecards/recompute', { as_of_date: sectorFlowDate.value || undefined }, true); }
async function syncCninfoAnnouncements() { const symbols = conceptCandidates.value.slice(0, 20).map((item) => item.symbol); await runAction('同步巨潮公告', '/api/research/events/cninfo/sync', { symbols, universe_key: 'core', lookback_days: 45, max_pages_per_symbol: 1 }, true); }
async function studyConceptCandidate(symbol: string) { studySymbol.value = symbol; activeResearchTab.value = 'stock-study'; await runStockStudy(); }
async function reconcileStaleFetchRuns() { await runAction('修复陈旧运行任务', '/api/research/operations/fetch-runs/reconcile-stale', { max_age_minutes: 90, terminal_status: 'failed' }, true); }
async function decideReview(item: ClaimReview, status: 'approved' | 'rejected') {
  if (status === 'approved' && !/^\d{6}\.(SH|SZ|BJ)$/.test((reviewSymbol.value[item.review_id] || item.suggested_symbol || '').toUpperCase())) { ElMessage.error('批准前请填写有效股票代码'); return; }
  await runAction(status === 'approved' ? '批准分析师标的映射' : '拒绝分析师标的映射', `/api/research/claim-review/${item.review_id}`, { status, symbol: (reviewSymbol.value[item.review_id] || item.suggested_symbol || '').toUpperCase() });
}
async function runFactorEvaluation() { await runAction('评估因子', '/api/research/factors/evaluate', { universe_key: 'all_a', factor_keys: selectedFactors.value, horizon_days: factorHorizon.value }); }
async function runStrategyBacktest() { await runAction('运行A股约束回测', '/api/research/strategies/backtest', { strategy_key: 'multi_factor_rank_v1', universe_key: 'all_a', factors: selectedFactors.value, ...backtestForm.value }); }
async function runMainWaveResearch() { await runAction('训练观察池主升影子模型', '/api/research/strategy/watchlist-main-wave/run', {}, true); }
function connectEvents() {
  eventSource?.close(); eventSource = new EventSource('/events');
  eventSource.addEventListener('snapshot', (event) => { events.value = JSON.parse((event as MessageEvent).data); connected.value = true; });
  eventSource.addEventListener('message', (event) => { const item: EventItem = JSON.parse((event as MessageEvent).data); events.value = [item, ...events.value.filter((current) => current.event_id !== item.event_id)].slice(0, 200); connected.value = true; });
  eventSource.onopen = () => { connected.value = true; retryDelay = 1000; };
  eventSource.onerror = () => { connected.value = false; eventSource?.close(); if (retryTimer) clearTimeout(retryTimer); retryTimer = window.setTimeout(connectEvents, retryDelay); retryDelay = Math.min(30_000, retryDelay * 2); };
}
function addFiles(list: FileList | File[]) { const incoming = Array.from(list); const allowed = incoming.filter((file) => file.size <= 500 * 1024 * 1024); if (allowed.length !== incoming.length) relayState.value = '超过 500 MB 的文件未加入'; relayFiles.value = [...relayFiles.value, ...allowed.filter((file) => !relayFiles.value.some((current) => current.name === file.name && current.size === file.size))]; }
function submitRelay() {
  if ((!relayText.value.trim() && !relayFiles.value.length) || !relayTag.value) { relayState.value = '请填写正文或选择媒体，并选择来源'; return; }
  const form = new FormData(); form.append('tag', relayTag.value); form.append('text', relayText.value.trim()); form.append('source_label', relaySource.value.trim()); if (relayDate.value) form.append('content_date', relayDate.value); if (relayTime.value) form.append('content_time', relayTime.value); relayFiles.value.forEach((file) => form.append('media', file, file.name));
  const xhr = new XMLHttpRequest(); relayXhr.value = xhr; relayState.value = '上传中'; relayProgress.value = 0; xhr.open('POST', '/manual-relay'); xhr.upload.onprogress = (event) => { if (event.lengthComputable) relayProgress.value = Math.round(event.loaded / event.total * 100); }; xhr.onload = () => { try { const body = JSON.parse(xhr.responseText); if (xhr.status >= 300) throw new Error(body.message); relayState.value = `已接收 ${body.message_id}`; relayText.value = ''; relayFiles.value = []; } catch (error) { relayState.value = `失败：${error instanceof Error ? error.message : String(error)}`; } relayXhr.value = null; }; xhr.onerror = () => { relayState.value = '网络错误'; relayXhr.value = null; }; xhr.send(form);
}
onMounted(() => {
  mobileMediaQuery.addEventListener('change', syncMobileLayout); loadConfig().catch(() => {}); connectEvents(); loadResearch();
  polling.every(15_000, () => { void loadRealtimeServices(); });
  polling.every(10_000, () => { void loadGroupRelayStatus(); });
  polling.every(10_000, () => { void loadFeishuWorkbench(); });
  void loadBoardFlowCurves(true); void loadMarketFlowFeatures(); void loadBoardRotationEvents(); void loadBoardStockMining(); void loadLimitLinkageMining(); polling.every(60_000, () => {
    if (document.visibilityState === 'visible' && boardFlowIsExchangeToday.value) { void loadBoardFlowCurves(false); void loadMarketFlowFeatures(); void loadBoardRotationEvents(); void loadBoardStockMining(); void loadLimitLinkageMining(); }
  });
});
onBeforeUnmount(() => {
  mobileMediaQuery.removeEventListener('change', syncMobileLayout); eventSource?.close();
  if (retryTimer) clearTimeout(retryTimer); polling.stop();
});

const dashboardBindings = {
    initialPath,
    mobileMediaQuery,
    mobileLayout,
    syncMobileLayout,
    activeSection,
    sharedResearchParams,
    sharedResearchSymbol,
    sharedResearchTab,
    activeResearchTab,
    routes,
    events,
    connected,
    eventFilter,
    relayTag,
    relaySource,
    relayText,
    relayFiles,
    relayDate,
    relayTime,
    relayState,
    relayProgress,
    relayXhr,
    loading,
    actionLoading,
    researchError,
    overview,
    reports,
    remoteMessages,
    analystSkills,
    analystResearchStatus,
    claims,
    providerHealth,
    providerApiCapabilities,
    marketSnapshots,
    sectors,
    sectorFlows,
    conceptSignals,
    conceptCandidates,
    announcements,
    lhbEvents,
    closeBoardReport,
    conceptBackfill,
    closeStrategyReview,
    postCloseStrategyRun,
    postCloseCandidates,
    strategyPatternRun,
    strategyLimitPool,
    strategyLimitLadder,
    strategyContinuationCandidates,
    strategyDragonLeaderCandidates,
    strategyDragonLeaderMarket,
    strategyPoolCoverage,
    strategyPatternPicks,
    strategyPatternSamples,
    postCloseRefresh,
    intradayOutcomes,
    intradayOutcomeSummary,
    intradayAttributionSummary,
    attributionValidationGate,
    analystReadiness,
    analystScorecards,
    selectedReviewBoardKey,
    catalog,
    recommendations,
    universe,
    featureItems,
    claimReviews,
    factors,
    factorEvaluations,
    strategies,
    strategyExperiments,
    mainWaveExperiments,
    frameworks,
    trainingRoadmap,
    qualityIssues,
    minuteImports,
    minuteDirectory,
    realtimeServices,
    adapterHealth,
    runtimeHealth,
    realtimeLoading,
    realtimeError,
    groupRelayStatus,
    groupRelayLoading,
    groupRelayError,
    groupRelayRouteDialog,
    groupRelayRouteSaving,
    groupRelayRouteForm,
    feishuWorkbench,
    feishuWorkbenchMessages,
    feishuWorkbenchLoading,
    feishuWorkbenchError,
    feishuWorkbenchAction,
    workbenchSearch,
    workbenchSearchResult,
    workbenchIntegrationDialog,
    workbenchIntegration,
    paperStatus,
    analystObservations,
    strategyFunnel,
    strategyGovernance,
    analystSyncHealth,
    analystPromptLab,
    analystMarketEvaluation,
    analystDailyReview,
    analystWeeklyReview,
    analystReviewRunning,
    analystReviewRuns,
    automationRuns,
    analystStockTimeline,
    analystStockTimelineLoading,
    analystStockTimelineError,
    analystTimelineAnalyst,
    analystTimelineDate,
    strategyAblation,
    strategyHealth,
    catalogQuery,
    catalogGroup,
    selectedCatalog,
    auditResults,
    catalogRefreshing,
    fetchDialogOpen,
    fetchResultOpen,
    fetchResult,
    fetchForm,
    studySymbol,
    studyLookback,
    stockStudy,
    studyLoading,
    studyError,
    universeText,
    universePriority,
    reviewSymbol,
    sectorMemberOffset,
    sectorMemberLimit,
    sectorFlowDate,
    chinaMinute,
    chinaDateTime,
    boardFlowTaxonomy,
    boardFlowDate,
    boardFlowSeries,
    boardFlowSnapshots,
    boardFlowCursor,
    boardFlowLoading,
    boardFlowError,
    boardFlowNotice,
    boardFlowFocus,
    boardFlowDisplaySlots,
    boardFlowIsExchangeToday,
    boardRotationEvents,
    boardStockMining,
    limitLinkageMining,
    marketFlow,
    marketFlowError,
    selectedFactors,
    factorHorizon,
    backtestForm,
    retryTimer,
    retryDelay,
    eventSource,
    polling,
    visibleEvents,
    catalogGroups,
    visibleCatalog,
    count,
    dateText,
    healthState,
    realtimeStateType,
    realtimeStateText,
    groupRelayStateType,
    groupRelayStateText,
    groupRelayMessageText,
    realtimeDeliveryDetail,
    ageText,
    bytesText,
    paperSectorExposureItems,
    claimDirection,
    studyStance,
    studyType,
    recommendationDirection,
    recommendationType,
    postCloseCandidateLabel,
    postCloseCandidateType,
    patternCohortLabel,
    sourceType,
    snapshotType,
    availabilityType,
    availabilityText,
    permissionText,
    policyText,
    catalogCount,
    observationText,
    displayValue,
    readinessType,
    outcomeStatusType,
    outcomePercent,
    moneyWan,
    reviewTierText,
    attributionDimensionLabel,
    attributionCohortLabel,
    attributionStatusType,
    analystReadinessText,
    historyDatasetRows,
    featureReadinessRows,
    storageText,
    rowText,
    featureStatusType,
    studyBars,
    studyMarketRecord,
    featureRecord,
    metricNumber,
    nestedValue,
    nestedNumber,
    latestFactorEvaluations,
    factorChartOption,
    latestExperiment,
    latestMainWaveExperiment,
    latestReboundExperiment,
    mainWaveCurrentScores,
    mainWaveQualification,
    mainWaveFailedChecks,
    reboundCurrentScores,
    reboundFailedChecks,
    reviewConceptBoards,
    selectedReviewBoard,
    selectedReviewBoardStocks,
    completedBackfillBoards,
    closeIndexRegime,
    closeShortTermReview,
    indexRegimeLabel,
    indexRegimeType,
    indexLabel,
    equityChartOption,
    analystStockTimelineChartOption,
    analystReviewChartOption,
    marketFlowLatest,
    marketFlowSectorHighlights,
    marketFlowStateLabel,
    marketFlowStateType,
    sectorFlowTransitionLabel,
    sectorFlowTransitionType,
    marketFlowChartOption,
    boardFlowSeriesRows,
    boardFlowLatestSnapshot,
    boardFlowLatestValues,
    boardFlowHighlighted,
    boardFlowWindowText,
    boardFlowGaps,
    boardRotationKind,
    boardRotationStateType,
    boardRotationStateText,
    boardRotationDeliveryText,
    boardFlowChartOption,
    loadConfig,
    loadBoardFlowCurves,
    loadMarketFlowFeatures,
    loadBoardRotationEvents,
    loadBoardStockMining,
    loadLimitLinkageMining,
    resetBoardFlowCurves,
    loadRealtimeServices,
    loadGroupRelayStatus,
    loadFeishuWorkbench,
    inspectFeishuApplication,
    workbenchMessageText,
    workbenchWorkflowText,
    oauthAuditLabel,
    oauthAuditTagType,
    relayDeliveryLabel,
    relayDeliveryTagType,
    ingestionDeliveryLabel,
    ingestionDeliveryTagType,
    applicationInspectionLabel,
    applicationInspectionTagType,
    targetChatInspectionLabel,
    targetChatInspectionTagType,
    capabilityAuthorizationLabel,
    capabilityAuthorizationTagType,
    runWorkbenchAction,
    searchFeishuMessages,
    openWorkbenchIntegration,
    runWorkbenchEndpoint,
    createWorkbenchDigest,
    createWorkbenchTab,
    submitWorkbenchIntegration,
    openCreateGroupRelayRoute,
    openEditGroupRelayRoute,
    saveGroupRelayRoute,
    setGroupRelayRouteEnabled,
    deleteGroupRelayRoute,
    loadResearch,
    runAction,
    runAnalystMarketReview,
    loadAnalystStockTimeline,
    openFetch,
    selectCatalog,
    refreshCatalog,
    auditSelectedCatalog,
    executeFetch,
    runStockStudy,
    probeRealtimeMinutes,
    probeAkshareSupplement,
    probeAkshareMacroSupplement,
    saveUniverse,
    syncAllMarketUniverse,
    runMarketSnapshot,
    syncFullMarketDaily,
    syncSectorDirectory,
    syncSectorMembers,
    syncSectorFlows,
    syncConceptSignals,
    syncConceptCandidates,
    runBoardResearch,
    refreshCloseReview,
    runPostCloseStrategy,
    runStrategyPatternMining,
    runPostCloseRefresh,
    advanceConceptBackfill,
    settleIntradayOutcomes,
    recomputeAnalystScorecards,
    syncCninfoAnnouncements,
    studyConceptCandidate,
    reconcileStaleFetchRuns,
    decideReview,
    runFactorEvaluation,
    runStrategyBacktest,
    runMainWaveResearch,
    connectEvents,
    addFiles,
    submitRelay,
}
provide('manual-relay', dashboardBindings);
provide(dashboardContextKey, dashboardBindings);
return proxyRefs(dashboardBindings);
}

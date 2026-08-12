<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { DataAnalysis, Document, Operation, Refresh, UploadFilled, WarningFilled } from '@element-plus/icons-vue';
import VChart from 'vue-echarts';
import { use } from 'echarts/core';
import { BarChart, LineChart } from 'echarts/charts';
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

use([BarChart, LineChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

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
type BoardRotationEvent = { rotation_event_id: string; taxonomy_key: string; sector_key: string; label: string; event_type: 'cross_zero' | 'flow_surge'; direction: 'inflow' | 'outflow'; state: 'confirming' | 'confirmed' | 'alerted' | 'expired'; first_observed_at: string; last_observed_at: string; conditions?: { previous_net_inflow?: number; current_net_inflow?: number; delta_net_inflow?: number; dynamic_threshold?: number; change_pct?: number | null }; delivery_status?: 'pending' | 'sent' | 'failed' | null; sent_at?: string | null; error_message?: string | null };
type BoardStockMiningCandidate = { rank: number; direction: 'inflow' | 'outflow'; setup_key: string; symbol: string; name?: string; label: string; score: number; board_net_inflow?: number; main_net_inflow?: number; volume_ratio?: number; turnover_rate?: number; pct_change?: number; risk_flags?: string[] };
type BoardStockMining = { run?: { observed_at?: string; status?: string; coverage?: { exact_complete_boards?: number; quoted_exact_members?: number; partial_or_unmapped_boards_skipped?: number }; summary?: { returned?: number; inflow_candidates?: number; outflow_candidates?: number; notice?: string } } | null; inflow?: BoardStockMiningCandidate[]; outflow?: BoardStockMiningCandidate[]; notice?: string };
type LimitLinkageCandidate = { rank: number; symbol: string; name?: string; score: number; shared_concepts: number; concept_labels?: string[]; leader_symbols?: string[]; leader_names?: string[]; pct_change?: number; main_net_inflow?: number; volume_ratio?: number; turnover_rate?: number; risk_flags?: string[] };
type LimitLinkageMining = { run?: { observed_at?: string; trade_date?: string; status?: string; summary?: { anchors?: number; exact_relation_rows?: number; candidate_count?: number } } | null; items?: LimitLinkageCandidate[]; notice?: string };
type BackfillState = { state: string; boards: number; members: number; latest_updated_at?: string };
type ConceptBackfill = { trade_date?: string | null; total_concepts: number; mapped_concepts: number; states: BackfillState[]; automatic?: { enabled: boolean; batch_size: number }; notice?: string };
type IndexRegimeItem = { symbol: string; trading_date?: string; close?: number; drawdown_high_to_low_pct?: number; rebound_from_low_pct?: number; versus_period_high_pct?: number; range_retracement?: number; return_5_sessions_pct?: number; volume_ratio_5_vs_prior15?: number };
type MultiIndexRegime = { state?: string; index_count?: number; median_range_retracement?: number; interpretation?: string; items?: IndexRegimeItem[] };
type IndexBreadthContext = Record<string, unknown> & { multi_index_regime?: MultiIndexRegime; quality_flags?: string[] };
type StrategyReview = { exchange_date?: string; session?: string; observed_at?: string; market_state?: string; report?: { index_breadth_context?: IndexBreadthContext; analyst_context?: Record<string, unknown>; data_boundary?: Record<string, unknown> } };
type PostCloseCandidate = { rank: number; symbol: string; name?: string; candidate_type: 'base_ready_30d' | 'base_forming_15d' | 'fresh_start_15d'; score: number; structure: { status?: string; score?: number; bar_count?: number; metrics?: Record<string, unknown>; notice?: string }; board_context: { label?: string; net_amount?: number; change_pct?: number; exact_member_mapping?: boolean }; risk_flags: string[] };
type PostCloseStrategyRun = { run_id?: string; as_of_date?: string; model_version?: string; status?: string; source_status?: Record<string, unknown>; summary?: Record<string, unknown>; updated_at?: string };
type LhbContext = { top_list_rows?: number; institution_records?: number; institution_count?: number; institution_buy?: number; institution_sell?: number; institution_net_buy?: number; institutions?: string[]; reasons?: string[] };
type StrategyPatternSample = { rank: number; symbol: string; name?: string; primary_cohort: string; cohorts: string[]; board_context: { label?: string; net_amount?: number; exact_member_mapping?: boolean }; limit_context: { tag?: string; status?: string; streak_count?: number; turnover_rate?: number; limit_pool_market_rank?: number; preopen_limit_pool_rank?: number; review_score?: number; review_tier?: string; selection_reasons?: string[]; lhb_context?: LhbContext | null }; daily_features: { low_pct?: number; close_pct?: number; volume_multiple_5d?: number; ground_to_sky_daily_shape?: boolean }; intraday_pattern: { status?: string; pattern_tags?: string[]; deep_reversal_impulse?: { time?: string } | null; deep_discount_stabilization?: { time?: string; confirmation?: string } | null; standard_ignition?: { time?: string } | null; opening_drive?: { first_four_pct_time?: string; first_eight_pct_time?: string; limit_reclaim_time?: string } | null; previous_close_reclaim?: { time?: string } | null; previous_close_acceptance?: { time?: string } | null; limit_reclaim?: { time?: string } | null }; minute_source?: string; risk_flags: string[] };
type StrategyPatternRun = { run_id?: string; as_of_date?: string; model_version?: string; status?: string; source_status?: Record<string, unknown>; summary?: Record<string, unknown>; updated_at?: string };
type LimitPoolRow = { rank: number; ts_code: string; name?: string; tag?: string; board_count?: number; status?: string; price?: number; pct_chg?: number; turnover_rate?: number; open_num?: number; limit_amount?: number; limit_up_suc_rate?: number; lu_desc?: string; volume_multiple_5d?: number; volume_multiple_20d?: number; sources?: string[]; board_context?: { label?: string; net_amount?: number; change_pct?: number } | null; lhb_context?: LhbContext | null };
type LimitLadderRow = LimitPoolRow & { nums?: string | number; ladder_sources?: string[] };
type LimitPoolCoverage = { status?: string; union_count?: number; intersection_count?: number; tushare_count?: number; eastmoney_count?: number; limit_step_count?: number; multi_board_union_count?: number; tushare_only?: string[]; eastmoney_only?: string[]; local_truncation?: boolean; notice?: string };
type PostCloseRefresh = { status?: string; trade_date?: string; daily_ready?: boolean; deferred_stages?: string[]; retry_hint?: string | null; finished_at?: string };
type IntradayAttribution = { model_version?: string; stage?: string; market_state?: string; sector_linkage?: string; volume_baseline?: string };
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
type AdapterHealth = { status?: string; quant_alert_configured?: boolean; events?: number };
type RemoteReport = { remote_report_id: string; analyst_name: string; report_date: string; title: string; summary: string; remote_version?: string };
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
type Factor = { factor_key: string; label: string; category: string; implementation: string; framework_tags: string[]; status: string; version: string };
type FactorEvaluation = { evaluation_id: string; factor_key: string; label: string; status: string; observations: number; cross_section_days: number; horizon_days: number; metrics: Record<string, unknown>; artifact?: Record<string, unknown>; created_at?: string };
type Strategy = { strategy_key: string; label: string; engine: string; version: string; configuration: Record<string, unknown>; status: string };
type StrategyExperiment = { strategy_experiment_id: string; strategy_key: string; label: string; status: string; metrics: Record<string, unknown>; parameters: Record<string, unknown>; equity_curve: { date: string; equity: number; return: number; positions: number }[]; trades: Record<string, unknown>[]; created_at?: string };
type Framework = { framework_key: string; label: string; role: string; integration_mode: string; status: string; license_note: string; prerequisites: string[] };
type TrainingRoadmap = { status: string; policy: string; stages: { stage: string; gate: string; compute: string }[] };

const initialPath = window.location.pathname;
const mobileMediaQuery = window.matchMedia('(max-width: 760px)');
const mobileLayout = ref(mobileMediaQuery.matches);
const syncMobileLayout = (event: MediaQueryListEvent) => { mobileLayout.value = event.matches; };
const activeSection = ref(initialPath === '/relay' ? 'relay' : initialPath === '/monitor' ? 'monitor' : 'research');
const sharedResearchParams = new URLSearchParams(window.location.search);
const sharedResearchSymbol = (sharedResearchParams.get('symbol') || '').toUpperCase();
const sharedResearchTab = sharedResearchParams.get('tab');
const activeResearchTab = ref(sharedResearchTab === 'stock-study' && /^\d{6}\.(SH|SZ|BJ)$/.test(sharedResearchSymbol) ? 'stock-study' : 'overview');
const routes = ref<Route[]>([]); const events = ref<EventItem[]>([]); const connected = ref(false); const eventFilter = ref('all');
const relayTag = ref(''); const relaySource = ref(''); const relayText = ref(''); const relayFiles = ref<File[]>([]); const relayDate = ref(''); const relayTime = ref(''); const relayState = ref(''); const relayProgress = ref(0); const relayXhr = ref<XMLHttpRequest | null>(null);
const loading = ref(false); const actionLoading = ref(''); const researchError = ref('');
const overview = ref<ResearchOverview>({}); const reports = ref<RemoteReport[]>([]); const claims = ref<AnalystClaim[]>([]); const providerHealth = ref<ProviderHealth[]>([]); const providerApiCapabilities = ref<ProviderApiCapability[]>([]); const marketSnapshots = ref<MarketSnapshot[]>([]); const sectors = ref<Sector[]>([]); const sectorFlows = ref<SectorFlow[]>([]); const conceptSignals = ref<ConceptSignal[]>([]); const conceptCandidates = ref<ConceptCandidate[]>([]); const announcements = ref<Announcement[]>([]); const lhbEvents = ref<Announcement[]>([]); const closeBoardReport = ref<BoardReviewReport | null>(null); const conceptBackfill = ref<ConceptBackfill>({ total_concepts: 0, mapped_concepts: 0, states: [] }); const closeStrategyReview = ref<StrategyReview | null>(null); const postCloseStrategyRun = ref<PostCloseStrategyRun | null>(null); const postCloseCandidates = ref<PostCloseCandidate[]>([]); const strategyPatternRun = ref<StrategyPatternRun | null>(null); const strategyLimitPool = ref<LimitPoolRow[]>([]); const strategyLimitLadder = ref<LimitLadderRow[]>([]); const strategyPoolCoverage = ref<LimitPoolCoverage>({}); const strategyPatternPicks = ref<StrategyPatternSample[]>([]); const strategyPatternSamples = ref<StrategyPatternSample[]>([]); const postCloseRefresh = ref<PostCloseRefresh | null>(null); const intradayOutcomes = ref<IntradayOutcome[]>([]); const intradayOutcomeSummary = ref<IntradayOutcomeSummary[]>([]); const intradayAttributionSummary = ref<IntradayAttributionSummary[]>([]); const attributionValidationGate = ref<AttributionValidationGate>({ status: 'accumulating', matured_unique_signals: 0, trading_days: 0, required_unique_signals: 200, required_trading_days: 60 }); const analystReadiness = ref<AnalystReadiness[]>([]); const analystScorecards = ref<AnalystScorecard[]>([]); const selectedReviewBoardKey = ref(''); const catalog = ref<{ count?: number; counts?: CatalogCounts; items?: CatalogItem[]; providers?: ProviderConfig[]; online_range_max_days?: number; historical_minute_policy?: string; realtime_minute_policy?: string; coverage_rule?: string }>({}); const recommendations = ref<Recommendation[]>([]); const universe = ref<UniverseMember[]>([]); const featureItems = ref<FeatureItem[]>([]); const claimReviews = ref<ClaimReview[]>([]); const factors = ref<Factor[]>([]); const factorEvaluations = ref<FactorEvaluation[]>([]); const strategies = ref<Strategy[]>([]); const strategyExperiments = ref<StrategyExperiment[]>([]); const frameworks = ref<Framework[]>([]); const trainingRoadmap = ref<TrainingRoadmap>({ status: 'planned', policy: '', stages: [] }); const qualityIssues = ref<QualityIssue[]>([]); const minuteImports = ref<MinuteImport[]>([]); const minuteDirectory = ref('');
const realtimeServices = ref<RealtimeServiceStatus>({ items: [] }); const adapterHealth = ref<AdapterHealth>({}); const realtimeLoading = ref(false); const realtimeError = ref('');
const catalogQuery = ref(''); const catalogGroup = ref('all'); const selectedCatalog = ref<CatalogItem[]>([]); const auditResults = ref<CapabilityAuditRow[]>([]); const catalogRefreshing = ref(false); const fetchDialogOpen = ref(false); const fetchResultOpen = ref(false); const fetchResult = ref<Record<string, unknown>>({}); const fetchForm = ref({ api_name: 'daily', provider: 'auto', paramsText: '{\n  "ts_code": "000001.SZ",\n  "start_date": "20260804",\n  "end_date": "20260804"\n}', fields: 'ts_code,trade_date,open,high,low,close,vol,amount', max_rows: 100 });
const studySymbol = ref(/^\d{6}\.(SH|SZ|BJ)$/.test(sharedResearchSymbol) ? sharedResearchSymbol : '000636.SZ'); const studyLookback = ref(21); const stockStudy = ref<StockStudy | null>(null); const studyLoading = ref(false); const studyError = ref('');
const universeText = ref(''); const universePriority = ref(100); const reviewSymbol = ref<Record<string, string>>({}); const sectorMemberOffset = ref(0); const sectorMemberLimit = ref(10); const sectorFlowDate = ref('');
const chinaMinute = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '-';
const chinaDateTime = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '-';
const boardFlowTaxonomy = ref<'industry' | 'concept'>('industry'); const boardFlowDate = ref(''); const boardFlowSeries = ref<Record<string, BoardFlowSeries>>({}); const boardFlowSnapshots = ref<BoardFlowSnapshot[]>([]); const boardFlowCursor = ref<string | null>(null); const boardFlowLoading = ref(false); const boardFlowError = ref(''); const boardFlowNotice = ref(''); const boardFlowFocus = ref<string[]>([]); const boardFlowDisplaySlots = ref<string[]>([]); const boardFlowIsExchangeToday = ref(false); const boardRotationEvents = ref<BoardRotationEvent[]>([]); const boardStockMining = ref<BoardStockMining>({}); const limitLinkageMining = ref<LimitLinkageMining>({});
const selectedFactors = ref<string[]>([]); const factorHorizon = ref(5); const backtestForm = ref({ rebalance_days: 5, hold_days: 5, top_n: 20, total_cost_bps: 18 });
let retryTimer: number | undefined; let realtimeTimer: number | undefined; let boardFlowTimer: number | undefined; let retryDelay = 1000; let eventSource: EventSource | undefined;

const visibleEvents = computed(() => eventFilter.value === 'all' ? events.value : events.value.filter((item) => item.n8n_status === eventFilter.value));
const catalogGroups = computed(() => ['all', ...Array.from(new Set((catalog.value.items ?? []).map((item) => item.group)))]);
const visibleCatalog = computed(() => (catalog.value.items ?? []).filter((item) => (catalogGroup.value === 'all' || item.group === catalogGroup.value) && (!catalogQuery.value || `${item.api_name} ${item.group} ${item.model_role} ${item.request_policy}`.toLowerCase().includes(catalogQuery.value.toLowerCase()))));
const count = (name: string) => overview.value.counts?.[name] ?? 0;
const dateText = (value?: string | null) => value ? new Date(value).toLocaleString() : '未运行';
const healthState = (provider: ProviderHealth) => provider.circuit_open_until ? 'danger' : provider.last_error ? 'warning' : provider.last_success_at ? 'success' : 'info';
const realtimeStateType = (state?: RealtimeServiceState): 'success' | 'warning' | 'danger' | 'info' => state === 'healthy' || state === 'ready' ? 'success' : state === 'starting' || state === 'standby' ? 'warning' : state === 'degraded' || state === 'disabled' ? 'danger' : 'info';
const realtimeStateText = (state?: RealtimeServiceState) => ({ healthy: '运行正常', ready: '投递就绪', standby: '待命', starting: '启动中', degraded: '降级/延迟', disabled: '未配置', unavailable: '明确不可用' }[state ?? 'disabled']);
const realtimeDeliveryDetail = (service: RealtimeService) => {
  const details = service.details ?? {};
  if (service.key === 'feishu_alert') return `最近 ${details.latest_delivery_kind ?? '无'} / ${details.latest_delivery_status ?? '无'}；待重试 ${details.pending_retry_count ?? 0}；带外关注 ${details.meta_alert_state ?? 'normal'}`;
  if (service.key === 'daily_strategy_summary') return `最近交易日 ${details.latest_exchange_date ?? '尚无'}；投递 ${details.latest_delivery_status ?? '尚无'}；尝试 ${details.attempt_count ?? 0}/3`;
  return '';
};
const ageText = (seconds?: number | null) => seconds === null || seconds === undefined ? '-' : seconds < 60 ? `${Math.round(seconds)} 秒` : seconds < 3600 ? `${(seconds / 60).toFixed(1)} 分钟` : `${(seconds / 3600).toFixed(1)} 小时`;
const claimDirection = (value: number) => value > 0 ? '偏多' : value < 0 ? '偏空' : '中性';
const studyStance = (value?: string) => value === 'research_positive' ? '研究偏正面' : value === 'research_negative' ? '研究偏负面' : '证据混合或不足';
const studyType = (value?: string) => value === 'research_positive' ? 'success' : value === 'research_negative' ? 'danger' : 'info';
const recommendationDirection = (value?: number) => value && value > 0 ? '偏多' : value && value < 0 ? '偏空' : '中性';
const recommendationType = (value?: number) => value && value > 0 ? 'success' : value && value < 0 ? 'danger' : 'info';
const postCloseCandidateLabel = (value: PostCloseCandidate['candidate_type']) => value === 'base_ready_30d' ? '30日蓄势就绪' : value === 'base_forming_15d' ? '15日形成中' : '15日首动';
const postCloseCandidateType = (value: PostCloseCandidate['candidate_type']) => value === 'base_ready_30d' ? 'success' : value === 'fresh_start_15d' ? 'warning' : 'info';
const patternCohortLabel = (value: string) => ({ focus: '重点研究', ground_to_sky: '地天反转', preopen_market_leader: '盘前辨识度', market_leader: '盘后辨识度', board_leader: '板块龙头', consecutive_limit: '连板梯队', first_board: '首板' }[value] ?? value);
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
const attributionDimensionLabel = (value: string) => ({ model_version: '模型版本', stage: '信号阶段', market_state: '市场环境', sector_linkage: '板块联动', volume_baseline: '同刻量能' }[value] ?? value);
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
const factorChartOption = computed(() => ({
  tooltip: { trigger: 'axis' }, grid: { left: 48, right: 18, top: 24, bottom: 62 }, xAxis: { type: 'category', data: factorEvaluations.value.slice(0, 16).map((item) => item.label || item.factor_key), axisLabel: { rotate: 35 } }, yAxis: { type: 'value', name: 'Rank IC' }, series: [{ type: 'bar', data: factorEvaluations.value.slice(0, 16).map((item) => Number(item.metrics.rank_ic_mean) || 0), itemStyle: { color: '#1976d2' } }],
}));
const latestExperiment = computed(() => strategyExperiments.value[0] ?? null);
const reviewConceptBoards = computed(() => (closeBoardReport.value?.payload?.items ?? [])
  .filter((item) => item.taxonomy_key === 'ths_concept_flow' && item.mapped_members > 0)
  .sort((left, right) => Number(right.net_inflow ?? -Infinity) - Number(left.net_inflow ?? -Infinity)));
const selectedReviewBoard = computed(() => reviewConceptBoards.value.find((item) => item.sector_key === selectedReviewBoardKey.value) ?? reviewConceptBoards.value[0] ?? null);
const selectedReviewBoardStocks = computed(() => selectedReviewBoard.value?.top_stocks ?? []);
const completedBackfillBoards = computed(() => conceptBackfill.value.states.filter((item) => item.state === 'completed' || item.state === 'empty').reduce((total, item) => total + Number(item.boards || 0), 0));
const closeIndexRegime = computed(() => closeStrategyReview.value?.report?.index_breadth_context?.multi_index_regime ?? null);
const indexRegimeLabel = computed(() => ({
  corrective_rebound: '纠错反弹情景', trend_recovery: '趋势修复', weak_or_declining: '弱势/下行', mixed_transition: '混合过渡', insufficient_index_history: '历史不足',
}[closeIndexRegime.value?.state ?? ''] ?? closeIndexRegime.value?.state ?? '待生成'));
const indexRegimeType = computed((): 'success' | 'warning' | 'danger' | 'info' => closeIndexRegime.value?.state === 'trend_recovery' ? 'success' : closeIndexRegime.value?.state === 'corrective_rebound' || closeIndexRegime.value?.state === 'mixed_transition' ? 'warning' : closeIndexRegime.value?.state === 'weak_or_declining' ? 'danger' : 'info');
const indexLabel = (symbol: string) => ({ '000001.SH': '上证指数', '000300.SH': '沪深300', '399001.SZ': '深证成指', '399006.SZ': '创业板指' }[symbol] ?? symbol);
const equityChartOption = computed(() => ({
  tooltip: { trigger: 'axis' }, grid: { left: 48, right: 18, top: 24, bottom: 40 }, xAxis: { type: 'category', data: latestExperiment.value?.equity_curve.map((item) => item.date) ?? [] }, yAxis: { type: 'value', name: '净值', scale: true }, series: [{ type: 'line', smooth: true, showSymbol: false, data: latestExperiment.value?.equity_curve.map((item) => item.equity) ?? [], lineStyle: { width: 2, color: '#00897b' }, areaStyle: { color: 'rgba(0,137,123,0.12)' } }],
}));
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
const boardRotationStateText = (value: BoardRotationEvent['state']) => ({ confirming: '待下一分钟确认', confirmed: '已确认待投递', alerted: '飞书已送达', expired: '方向未延续' }[value] ?? value);
const boardRotationDeliveryText = (item: BoardRotationEvent) => item.delivery_status === 'sent' ? '已送达' : item.delivery_status === 'failed' ? '投递失败待重试' : item.delivery_status === 'pending' ? '待投递' : '尚未入队';
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

async function getJson<T>(path: string): Promise<T> { const response = await fetch(path); if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); }
async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> { const response = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error((data as { detail?: string; message?: string }).detail ?? (data as { message?: string }).message ?? `HTTP ${response.status}`); return data as T; }
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
function resetBoardFlowCurves() { void loadBoardFlowCurves(true); }
async function loadRealtimeServices() {
  realtimeLoading.value = true; realtimeError.value = '';
  try {
    const [services, adapter] = await Promise.all([
      getJson<RealtimeServiceStatus>('/api/research/intraday/services/status'),
      getJson<AdapterHealth>('/health'),
    ]);
    realtimeServices.value = services; adapterHealth.value = adapter;
    const feishu = realtimeServices.value.items?.find((item) => item.key === 'feishu_alert');
    if (feishu && (adapter.status !== 'ok' || !adapter.quant_alert_configured)) {
      feishu.state = adapter.status === 'ok' ? 'disabled' : 'degraded';
      feishu.last_error = adapter.status === 'ok' ? '飞书提醒目标或内部鉴权未配置' : '飞书适配器健康检查失败';
    }
  } catch (error) {
    realtimeError.value = error instanceof Error ? error.message : String(error);
  } finally { realtimeLoading.value = false; }
}
async function loadResearch() {
  loading.value = true; researchError.value = '';
  try {
    const [overviewData, reportsData, claimsData, healthData, capabilityData, catalogData, snapshotData, sectorData, sectorFlowData, conceptSignalData, conceptCandidateData, announcementData, lhbData, boardReviewData, backfillData, strategyReviewData, postCloseStrategyData, patternData, recommendationData, universeData, featuresData, reviewsData, factorData, evaluationData, strategyData, experimentData, frameworkData, roadmapData, qualityData, minuteData] = await Promise.all([
      getJson<ResearchOverview>('/api/research/overview'), getJson<{ items?: RemoteReport[] }>('/api/research/reports?limit=30'), getJson<{ items?: AnalystClaim[] }>('/api/research/claims?limit=80'), getJson<{ items?: ProviderHealth[] }>('/api/research/providers'), getJson<{ items?: ProviderApiCapability[] }>('/api/research/provider-capabilities'), getJson<typeof catalog.value>('/api/research/tushare/catalog'), getJson<{ items?: MarketSnapshot[] }>('/api/research/market/snapshots?limit=20'), getJson<{ items?: Sector[] }>('/api/research/market/sectors?taxonomy_key=ths_index_n&limit=500'), getJson<{ items?: SectorFlow[] }>('/api/research/market/sector-flows?taxonomy_key=ths_industry&limit=100'), getJson<{ items?: ConceptSignal[] }>('/api/research/market/sectors/concepts?limit=100'), getJson<{ items?: ConceptCandidate[] }>('/api/research/market/sectors/concepts/candidates?limit=100'), getJson<{ items?: Announcement[] }>('/api/research/events/announcements?limit=100'), getJson<{ items?: Announcement[] }>('/api/research/events/lhb?limit=100'), getJson<{ report?: BoardReviewReport | null }>('/api/research/market/sectors/review/report/latest'), getJson<ConceptBackfill>('/api/research/market/sectors/concepts/members/backfill/status'), getJson<{ review?: StrategyReview | null }>('/api/research/strategy/reviews/latest?session=close'), getJson<{ run?: PostCloseStrategyRun | null; candidates?: PostCloseCandidate[] }>('/api/research/strategy/post-close/latest'), getJson<{ run?: StrategyPatternRun | null; limit_pool?: LimitPoolRow[]; limit_ladder?: LimitLadderRow[]; picks?: StrategyPatternSample[]; samples?: StrategyPatternSample[] }>('/api/research/strategy/pattern-mining/latest'), getJson<{ recommendations?: Recommendation[] }>('/api/research/recommendations'), getJson<{ items?: UniverseMember[] }>('/api/research/universes/core'), getJson<{ items?: FeatureItem[] }>('/api/research/features/latest?universe_key=core'), getJson<{ items?: ClaimReview[] }>('/api/research/claim-review?status=pending'), getJson<{ items?: Factor[] }>('/api/research/factors'), getJson<{ items?: FactorEvaluation[] }>('/api/research/factor-evaluations?universe_key=core'), getJson<{ items?: Strategy[] }>('/api/research/strategies'), getJson<{ items?: StrategyExperiment[] }>('/api/research/strategy-experiments?universe_key=core'), getJson<{ items?: Framework[] }>('/api/research/frameworks'), getJson<TrainingRoadmap>('/api/research/training/roadmap'), getJson<{ items?: QualityIssue[] }>('/api/research/quality?limit=100'), getJson<{ items?: MinuteImport[]; offline_directory?: string }>('/api/research/minute/imports'),
    ]);
    overview.value = overviewData; reports.value = reportsData.items ?? []; claims.value = claimsData.items ?? []; providerHealth.value = healthData.items ?? []; providerApiCapabilities.value = capabilityData.items ?? []; catalog.value = catalogData; marketSnapshots.value = snapshotData.items ?? []; sectors.value = sectorData.items ?? []; sectorFlows.value = sectorFlowData.items ?? []; conceptSignals.value = conceptSignalData.items ?? []; conceptCandidates.value = conceptCandidateData.items ?? []; announcements.value = announcementData.items ?? []; lhbEvents.value = lhbData.items ?? []; closeBoardReport.value = boardReviewData.report ?? null; conceptBackfill.value = backfillData; closeStrategyReview.value = strategyReviewData.review ?? null; postCloseStrategyRun.value = postCloseStrategyData.run ?? null; postCloseCandidates.value = postCloseStrategyData.candidates ?? []; strategyPatternRun.value = patternData.run ?? null; strategyLimitPool.value = patternData.limit_pool ?? []; strategyLimitLadder.value = patternData.limit_ladder ?? []; strategyPoolCoverage.value = patternData.pool_coverage ?? {}; strategyPatternPicks.value = patternData.picks ?? []; strategyPatternSamples.value = patternData.samples ?? []; recommendations.value = recommendationData.recommendations ?? []; universe.value = universeData.items ?? []; featureItems.value = featuresData.items ?? []; claimReviews.value = reviewsData.items ?? []; factors.value = factorData.items ?? []; factorEvaluations.value = evaluationData.items ?? []; strategies.value = strategyData.items ?? []; strategyExperiments.value = experimentData.items ?? []; frameworks.value = frameworkData.items ?? []; trainingRoadmap.value = roadmapData; qualityIssues.value = qualityData.items ?? []; minuteImports.value = minuteData.items ?? []; minuteDirectory.value = minuteData.offline_directory ?? '';
    const [outcomeData, scorecardData] = await Promise.all([
      getJson<{ items?: IntradayOutcome[]; summary?: IntradayOutcomeSummary[]; attribution_summary?: IntradayAttributionSummary[]; attribution_validation_gate?: AttributionValidationGate }>('/api/research/intraday/outcomes/latest?limit=100'),
      getJson<{ items?: AnalystScorecard[]; readiness?: AnalystReadiness[] }>('/api/research/analyst-scorecards'),
    ]);
    intradayOutcomes.value = outcomeData.items ?? []; intradayOutcomeSummary.value = outcomeData.summary ?? [];
    intradayAttributionSummary.value = outcomeData.attribution_summary ?? [];
    attributionValidationGate.value = outcomeData.attribution_validation_gate ?? attributionValidationGate.value;
    analystScorecards.value = scorecardData.items ?? []; analystReadiness.value = scorecardData.readiness ?? [];
    if (!universeText.value) universeText.value = universe.value.filter((item) => item.enabled).map((item) => item.symbol).join(', ');
    if (!sectorFlowDate.value) sectorFlowDate.value = sectorFlows.value[0]?.trading_date ?? overview.value.latest_market_snapshot?.exchange_date ?? '';
    if (!selectedFactors.value.length) selectedFactors.value = factors.value.filter((item) => item.implementation === 'native').map((item) => item.factor_key);
  } catch (error) { researchError.value = error instanceof Error ? error.message : String(error); } finally { loading.value = false; }
}
async function runAction(label: string, path: string, body: Record<string, unknown> = {}, confirmation = true) {
  if (confirmation) await ElMessageBox.confirm(`确认执行${label}？`, '研究操作', { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' });
  actionLoading.value = label;
  try { const result = await postJson<Record<string, unknown>>(path, body); ElMessage.success(`${label}：${String(result.status ?? '已提交')}`); await loadResearch(); return result; } catch (error) { if (error !== 'cancel') ElMessage.error(`${label}失败：${error instanceof Error ? error.message : String(error)}`); return undefined; } finally { actionLoading.value = ''; }
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
async function runFactorEvaluation() { await runAction('评估因子', '/api/research/factors/evaluate', { universe_key: 'core', factor_keys: selectedFactors.value, horizon_days: factorHorizon.value }); }
async function runStrategyBacktest() { await runAction('运行A股约束回测', '/api/research/strategies/backtest', { strategy_key: 'multi_factor_rank_v1', universe_key: 'core', factors: selectedFactors.value.filter((item) => item !== 'moneyflow_dc_rate'), ...backtestForm.value }); }
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
  void loadRealtimeServices(); realtimeTimer = window.setInterval(() => { void loadRealtimeServices(); }, 15_000);
  void loadBoardFlowCurves(true); void loadBoardRotationEvents(); void loadBoardStockMining(); void loadLimitLinkageMining(); boardFlowTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && boardFlowIsExchangeToday.value) { void loadBoardFlowCurves(false); void loadBoardRotationEvents(); void loadBoardStockMining(); void loadLimitLinkageMining(); }
  }, 60_000);
});
onBeforeUnmount(() => {
  mobileMediaQuery.removeEventListener('change', syncMobileLayout); eventSource?.close();
  if (retryTimer) clearTimeout(retryTimer); if (realtimeTimer) clearInterval(realtimeTimer); if (boardFlowTimer) clearInterval(boardFlowTimer);
});
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="236px" class="side-nav">
      <div class="brand"><el-icon><DataAnalysis /></el-icon><div><strong>Quant Research</strong><span>投研与市场数据</span></div></div>
      <el-menu :default-active="activeSection" class="menu" @select="(value: string) => activeSection = value">
        <el-menu-item index="research"><el-icon><DataAnalysis /></el-icon><span>量化研究台</span></el-menu-item>
        <el-menu-item index="monitor"><el-icon><Operation /></el-icon><span>导入监控</span></el-menu-item>
        <el-menu-item index="relay"><el-icon><UploadFilled /></el-icon><span>手动投递</span></el-menu-item>
      </el-menu>
      <div class="side-state"><el-tag :type="connected ? 'success' : 'warning'" effect="plain">{{ connected ? '事件流已连接' : '事件流重连中' }}</el-tag></div>
    </el-aside>
    <el-container>
      <el-header class="topbar"><div><h1>{{ activeSection === 'research' ? '量化研究台' : activeSection === 'monitor' ? '导入监控' : '手动投递' }}</h1><span>{{ activeSection === 'research' ? '分析师证据、市场数据与研究候选池' : '本地持久化导入链路' }}</span></div><el-button :icon="Refresh" :loading="loading" @click="loadResearch">刷新数据</el-button></el-header>
      <el-main class="content">
        <template v-if="activeSection === 'research'">
          <el-alert v-if="researchError" :title="researchError" type="error" show-icon :closable="false" class="section-gap" />
          <el-tabs v-model="activeResearchTab" class="research-tabs">
            <el-tab-pane label="研究概览" name="overview">
              <el-row :gutter="14" class="metric-row"><el-col v-for="metric in [{label:'远端报告',value:count('remote_reports')},{label:'结构化观点',value:count('claims')},{label:'标准日线',value:count('canonical_bars')},{label:'质量问题',value:count('quality_issues')}]" :key="metric.label" :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong></el-card></el-col></el-row>
              <el-card shadow="never" class="section-gap" header="盘后一键更新">
                <el-alert title="按依赖顺序刷新全A基准与日线、腾讯收盘快照、AKShare/东财补充、同花顺资金流、巨潮公告、龙虎榜背景、板块复盘、分析师结算和盘后策略。某源尚未发布时其余步骤仍会完成，并会显示待重试项。" type="info" :closable="false" show-icon/>
                <div class="card-actions">
                  <el-button type="primary" :icon="Refresh" :loading="actionLoading === '盘后一键更新'" @click="runPostCloseRefresh">盘后一键更新</el-button>
                  <el-tag v-if="postCloseRefresh" :type="postCloseRefresh.status === 'completed' ? 'success' : 'warning'">{{ postCloseRefresh.status }}</el-tag>
                  <el-text v-if="postCloseRefresh?.trade_date" type="info">交易日 {{ postCloseRefresh.trade_date }} · 日线{{ postCloseRefresh.daily_ready ? '已就绪' : '待发布/待重试' }}</el-text>
                  <el-text v-if="postCloseRefresh?.deferred_stages?.length" type="warning">待处理：{{ postCloseRefresh.deferred_stages.join('、') }}</el-text>
                </div>
                <el-text v-if="postCloseRefresh?.retry_hint" type="warning">{{ postCloseRefresh.retry_hint }}</el-text>
              </el-card>
              <el-row :gutter="14">
                <el-col :md="8" :xs="24">
                  <el-card shadow="never" header="历史数据容量评估">
                    <el-statistic title="3年P0/P1日频估算" :value="Number(overview.history_estimate?.estimated_storage_gib ?? 0)" suffix="GiB" :precision="2"/>
                    <el-text type="info">不含分钟线；历史分钟仍只走离线文件。</el-text>
                    <el-table :data="historyDatasetRows" size="small" max-height="238" class="section-gap">
                      <el-table-column prop="label" label="数据集" min-width="120" show-overflow-tooltip/>
                      <el-table-column label="行数" width="95"><template #default="{ row }">{{ rowText(row.rows) }}</template></el-table-column>
                      <el-table-column label="存储" width="82"><template #default="{ row }">{{ storageText(row.estimated_storage_gib) }}</template></el-table-column>
                    </el-table>
                  </el-card>
                </el-col>
                <el-col :md="8" :xs="24">
                  <el-card shadow="never" header="当前覆盖">
                    <el-descriptions :column="1" border size="small">
                      <el-descriptions-item label="日线区间">{{ displayValue(overview.data_coverage?.first_bar_date) }} - {{ displayValue(overview.data_coverage?.latest_bar_date) }}</el-descriptions-item>
                      <el-descriptions-item label="有效交易日">{{ rowText(overview.data_coverage?.bar_days) }}</el-descriptions-item>
                      <el-descriptions-item label="全截面天数">{{ rowText(overview.data_coverage?.full_cross_section_days) }}</el-descriptions-item>
                      <el-descriptions-item label="最大单日股票数">{{ rowText(overview.data_coverage?.max_symbols_on_day) }}</el-descriptions-item>
                      <el-descriptions-item label="分钟覆盖股票">{{ rowText(overview.data_coverage?.minute_symbols) }}</el-descriptions-item>
                    </el-descriptions>
                  </el-card>
                </el-col>
                <el-col :md="8" :xs="24">
                  <el-card shadow="never" header="运行健康">
                    <el-descriptions :column="1" border size="small">
                      <el-descriptions-item label="运行中任务">{{ count('running_fetch_runs') }}</el-descriptions-item>
                      <el-descriptions-item label="陈旧任务"><el-tag :type="readinessType(count('stale_fetch_runs'))">{{ count('stale_fetch_runs') }}</el-tag></el-descriptions-item>
                      <el-descriptions-item label="全市场股票">{{ rowText(count('all_a_symbols')) }}</el-descriptions-item>
                      <el-descriptions-item label="板块成分">{{ rowText(count('active_sector_memberships')) }}</el-descriptions-item>
                    </el-descriptions>
                    <div class="card-actions"><el-button :disabled="!count('stale_fetch_runs')" :loading="actionLoading === '修复陈旧运行任务'" @click="reconcileStaleFetchRuns">修复陈旧任务</el-button></div>
                  </el-card>
                </el-col>
              </el-row>
              <el-row :gutter="14"><el-col :md="14" :xs="24"><el-card shadow="never" header="数据快照"><template v-if="overview.latest_snapshot"><el-descriptions :column="1" border><el-descriptions-item label="状态"><el-tag :type="overview.latest_snapshot.status === 'ready' ? 'success' : 'warning'">{{ overview.latest_snapshot.status }}</el-tag></el-descriptions-item><el-descriptions-item label="截至日期">{{ overview.latest_snapshot.as_of_date }}</el-descriptions-item><el-descriptions-item label="知识截止">{{ dateText(overview.latest_snapshot.knowledge_cutoff) }}</el-descriptions-item></el-descriptions></template><el-empty v-else description="尚无研究快照" :image-size="72" /><div class="card-actions"><el-button :loading="actionLoading === '构建快照'" @click="runAction('构建快照','/api/research/snapshots/build')">构建快照</el-button><el-button type="primary" :loading="actionLoading === '运行日常管线'" @click="runAction('运行日常管线','/api/research/pipeline/daily')">运行日常管线</el-button></div></el-card></el-col><el-col :md="10" :xs="24"><el-card shadow="never" header="最新候选池"><el-empty v-if="!recommendations.length" description="没有可展示候选" :image-size="72" /><el-table v-else :data="recommendations.slice(0, 5)" size="small"><el-table-column prop="rank" label="#" width="48"/><el-table-column prop="symbol" label="标的"/><el-table-column prop="score" label="评分" width="70"/><el-table-column prop="decision" label="结论"/></el-table></el-card></el-col></el-row>
              <el-card shadow="never" header="研究运行"><el-space wrap><el-button :loading="actionLoading === '重算远端报告观点'" @click="runAction('重算远端报告观点','/api/research/reports/reprocess',{ limit: 100 }, true)">重算远端报告观点</el-button><el-button :loading="actionLoading === '重算观点结果'" @click="runAction('重算观点结果','/api/research/outcomes/recompute')">重算观点结果</el-button><el-button :loading="actionLoading === '重算分析师评分卡'" @click="runAction('重算分析师评分卡','/api/research/scorecards/recompute')">重算分析师评分卡</el-button><el-tag type="info">原始记录 {{ count('tushare_raw_records') }}</el-tag><el-tag type="info">离线分钟 {{ count('offline_minute_bars') }}</el-tag></el-space></el-card>
              <el-card shadow="never" header="特征覆盖 Readiness">
                <el-alert :title="overview.feature_readiness?.decision_ready ? '核心决策数据已通过当前门槛' : `仍有阻塞项：${(overview.feature_readiness?.blockers ?? []).join(', ') || '未知'}`" :type="overview.feature_readiness?.decision_ready ? 'success' : 'warning'" :closable="false" show-icon/>
                <el-table :data="featureReadinessRows" size="small" max-height="330" class="section-gap">
                  <el-table-column prop="feature" label="特征" min-width="130"/>
                  <el-table-column prop="priority" label="优先级" width="82"/>
                  <el-table-column label="状态" width="92"><template #default="{ row }"><el-tag :type="featureStatusType(row.status)">{{ row.status }}</el-tag></template></el-table-column>
                  <el-table-column label="覆盖对象" width="105"><template #default="{ row }">{{ rowText(row.symbols) }}</template></el-table-column>
                  <el-table-column label="记录数" width="110"><template #default="{ row }">{{ rowText(row.rows) }}</template></el-table-column>
                  <el-table-column label="覆盖率" width="92"><template #default="{ row }">{{ row.coverage === null || row.coverage === undefined ? '-' : `${Math.round(row.coverage * 10000) / 100}%` }}</template></el-table-column>
                  <el-table-column label="最新日期" width="120"><template #default="{ row }">{{ displayValue(row.latest_date) }}</template></el-table-column>
                </el-table>
              </el-card>
            </el-tab-pane>
            <el-tab-pane label="全市场快照" name="market-snapshots">
              <el-alert title="午盘与收盘快照默认不调用公开全市场报价；启用前须确认上游限频。公开报价仅用于市场补充，未经授权的实时分钟源不会进入推荐决策。" type="warning" :closable="false" show-icon />
              <el-row :gutter="14" class="section-gap">
                <el-col :md="8" :xs="24"><el-card shadow="never" header="全市场基准"><el-statistic title="已登记A股" :value="count('all_a_symbols')"/><el-text type="info">每日盘前从 stock_basic 刷新。</el-text><div class="card-actions"><el-button type="primary" :loading="actionLoading === '刷新全市场股票池'" @click="syncAllMarketUniverse">刷新股票池</el-button></div></el-card></el-col>
                <el-col :md="8" :xs="24"><el-card shadow="never" header="盘中补充"><el-statistic title="市场快照次数" :value="count('market_snapshot_runs')"/><el-text type="info">上海时间 11:35 自动运行。</el-text><div class="card-actions"><el-button :loading="actionLoading === '生成午盘全市场快照'" @click="runMarketSnapshot('midday')">生成午盘快照</el-button></div></el-card></el-col>
                <el-col :md="8" :xs="24"><el-card shadow="never" header="收盘确认"><template v-if="overview.latest_market_snapshot"><el-descriptions :column="1" border size="small"><el-descriptions-item label="状态"><el-tag :type="snapshotType(overview.latest_market_snapshot.status)">{{ overview.latest_market_snapshot.status }}</el-tag></el-descriptions-item><el-descriptions-item label="覆盖率">{{ Math.round(overview.latest_market_snapshot.coverage * 10000) / 100 }}%</el-descriptions-item><el-descriptions-item label="决策可用">{{ overview.latest_market_snapshot.decision_eligible ? '是' : '否' }}</el-descriptions-item></el-descriptions></template><el-empty v-else description="尚无市场快照" :image-size="56"/><div class="card-actions"><el-button :loading="actionLoading === '生成收盘全市场快照'" @click="runMarketSnapshot('close')">生成收盘快照</el-button><el-button type="primary" :loading="actionLoading === '同步全市场收盘日线'" @click="syncFullMarketDaily">同步日线</el-button></div></el-card></el-col>
              </el-row>
              <el-row :gutter="14">
                <el-col :md="8" :xs="24"><el-card shadow="never" header="同花顺板块目录"><el-statistic title="已登记板块" :value="count('sectors')"/><el-text type="info">目录来自 ths_index，保留分类类型和来源。</el-text><div class="card-actions"><el-button :loading="actionLoading === '同步同花顺板块目录'" @click="syncSectorDirectory">同步目录</el-button></div></el-card></el-col>
                <el-col :md="8" :xs="24"><el-card shadow="never" header="板块成分批次"><el-statistic title="活跃成分关系" :value="count('active_sector_memberships')"/><el-form inline class="section-gap"><el-form-item label="起点"><el-input-number v-model="sectorMemberOffset" :min="0" :step="sectorMemberLimit" controls-position="right"/></el-form-item><el-form-item label="数量"><el-input-number v-model="sectorMemberLimit" :min="1" :max="50" controls-position="right"/></el-form-item></el-form><div class="card-actions"><el-button :loading="actionLoading === '同步板块成分批次'" @click="syncSectorMembers">同步成分</el-button></div></el-card></el-col>
                <el-col :md="8" :xs="24"><el-card shadow="never" header="行业资金流"><el-statistic title="行业观测记录" :value="count('sector_market_observations')"/><el-date-picker v-model="sectorFlowDate" type="date" value-format="YYYY-MM-DD" class="section-gap"/><div class="card-actions"><el-button type="primary" :loading="actionLoading === '同步同花顺行业资金流'" @click="syncSectorFlows">同步资金流</el-button></div></el-card></el-col>
              </el-row>
              <el-card shadow="never" class="section-gap"><template #header><div class="card-header"><span>概念资金流与涨停候选</span><el-space><el-button :loading="actionLoading === '同步概念资金流与涨停强度'" @click="syncConceptSignals">同步概念信号</el-button><el-button :loading="actionLoading === '生成概念涨停候选'" @click="syncConceptCandidates">生成涨停候选</el-button><el-button :loading="actionLoading === '同步巨潮公告'" @click="syncCninfoAnnouncements">同步公告</el-button><el-button type="primary" :loading="actionLoading === '板块到个股一键研究'" @click="runBoardResearch">一键研究</el-button></el-space></div></template><el-alert title="候选仅由同日同花顺概念成分代码与同花顺涨停池代码精确相交得到；板块扫描不直接构成交易建议。" type="info" :closable="false" show-icon/><el-table :data="conceptSignals.slice(0, 20)" max-height="320" size="small" class="section-gap"><el-table-column prop="label" label="概念" min-width="130"/><el-table-column prop="net_amount" label="净流入" width="105"/><el-table-column label="涨跌幅" width="90"><template #default="{ row }">{{ displayValue(row.change_pct) }}%</template></el-table-column><el-table-column prop="leading_label" label="板块领涨" width="110"/><el-table-column prop="up_nums" label="涨停数" width="78"/><el-table-column prop="aggregate_score" label="扫描分" width="82"/><el-table-column label="证据" width="120"><template #default="{ row }"><el-tag size="small" type="info">{{ row.provider_key }}</el-tag></template></el-table-column></el-table><el-table :data="conceptCandidates" max-height="360" size="small" class="section-gap"><el-table-column prop="concept_label" label="高流入概念" min-width="135"/><el-table-column prop="board_net_amount" label="板块净流入" width="115"/><el-table-column prop="symbol" label="代码" width="105"/><el-table-column prop="name" label="涨停股" min-width="100"/><el-table-column prop="limit_tag" label="连板" width="85"/><el-table-column label="成分" width="86"><template #default="{ row }"><el-tag size="small" :type="row.membership_status === 'completed' || row.membership_status === 'unchanged' ? 'success' : 'warning'">{{ row.membership_status === 'completed' || row.membership_status === 'unchanged' ? '完整' : '截断' }}</el-tag></template></el-table-column><el-table-column label="涨幅" width="78"><template #default="{ row }">{{ displayValue(row.pct_change) }}%</template></el-table-column><el-table-column prop="limit_amount" label="封单额" width="105"/><el-table-column prop="description" label="涨停原因" min-width="210" show-overflow-tooltip/><el-table-column label="研究" width="88" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">技术分析</el-button></template></el-table-column></el-table></el-card>
              <el-card shadow="never" header="官方公告事件"><el-table :data="announcements" max-height="320" size="small"><el-table-column prop="symbol" label="代码" width="105"/><el-table-column prop="event_type" label="类型" width="130"/><el-table-column prop="occurred_at" label="披露时间" width="165"><template #default="{ row }">{{ dateText(row.occurred_at) }}</template></el-table-column><el-table-column prop="title" label="标题" min-width="280" show-overflow-tooltip/><el-table-column prop="source" label="来源" width="110"/><el-table-column label="文件" width="80"><template #default="{ row }"><el-link v-if="row.url" :href="row.url" target="_blank" type="primary">PDF</el-link><span v-else>-</span></template></el-table-column></el-table></el-card>
              <el-card shadow="never" header="同花顺行业资金流"><el-table :data="sectorFlows" max-height="360"><el-table-column prop="trading_date" label="交易日" width="105"/><el-table-column prop="label" label="行业" min-width="130"/><el-table-column prop="change_pct" label="涨跌幅" width="100"><template #default="{ row }">{{ displayValue(row.change_pct) }}%</template></el-table-column><el-table-column prop="net_amount" label="净流入" width="115"/><el-table-column prop="net_buy_amount" label="买入额" width="115"/><el-table-column prop="net_sell_amount" label="卖出额" width="115"/><el-table-column prop="leading_label" label="领涨股" width="115"/><el-table-column prop="constituent_count" label="公司数" width="85"/><el-table-column prop="provider_key" label="来源" width="130"/></el-table></el-card>
              <el-card shadow="never" header="同花顺板块目录"><el-table :data="sectors" max-height="320" size="small"><el-table-column prop="sector_key" label="板块代码" width="130"/><el-table-column prop="label" label="板块名称" min-width="200"/><el-table-column prop="active_members" label="已同步成分" width="110"/><el-table-column prop="updated_at" label="更新于" min-width="165"><template #default="{ row }">{{ dateText(row.updated_at) }}</template></el-table-column></el-table></el-card>
              <el-card shadow="never" header="午盘与收盘市场情况"><el-table :data="marketSnapshots" max-height="360"><el-table-column prop="exchange_date" label="交易日" width="110"/><el-table-column prop="session" label="时点" width="90"/><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="snapshotType(row.status)">{{ row.status }}</el-tag></template></el-table-column><el-table-column label="覆盖" width="110"><template #default="{ row }">{{ row.quote_count }} / {{ row.universe_count }}</template></el-table-column><el-table-column label="上涨 / 下跌" width="140"><template #default="{ row }">{{ displayValue(row.summary?.advancers) }} / {{ displayValue(row.summary?.decliners) }}</template></el-table-column><el-table-column label="中位涨跌" width="110"><template #default="{ row }">{{ displayValue(row.summary?.median_change_pct) }}%</template></el-table-column><el-table-column label="成交额" width="140"><template #default="{ row }">{{ displayValue(row.summary?.market_amount) }}</template></el-table-column><el-table-column label="决策" width="85"><template #default="{ row }"><el-tag :type="row.decision_eligible ? 'success' : 'info'">{{ row.decision_eligible ? '可用' : '补充' }}</el-tag></template></el-table-column><el-table-column label="质量标记" min-width="260"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.quality_flags ?? []" :key="flag" size="small" type="warning">{{ flag }}</el-tag></el-space></template></el-table-column></el-table></el-card>
              <el-card shadow="never" header="接口能力事实"><el-table :data="providerApiCapabilities" max-height="320" size="small"><el-table-column prop="label" label="来源"/><el-table-column prop="api_name" label="API"/><el-table-column prop="availability" label="状态" width="110"><template #default="{ row }"><el-tag :type="row.availability === 'verified' ? 'success' : row.availability === 'unsupported' || row.availability === 'failed' ? 'danger' : 'warning'">{{ row.availability }}</el-tag></template></el-table-column><el-table-column prop="frequency" label="频率" width="150"/><el-table-column label="决策" width="90"><template #default="{ row }">{{ row.decision_eligible ? '可参与' : '仅证据' }}</template></el-table-column><el-table-column prop="note" label="审计说明" show-overflow-tooltip/></el-table></el-card>
            </el-tab-pane>
            <el-tab-pane label="收盘复盘" name="close-review">
              <el-alert title="复盘页只读取已保存的板块报告、市场快照和龙虎榜。龙虎榜是收盘后公开的次日观察背景，不参与当天盘中评分。" type="info" :closable="false" show-icon/>
              <el-row :gutter="14" class="section-gap">
                <el-col :md="4" :xs="12"><el-card shadow="never" class="metric-card"><span>市场状态</span><strong class="review-metric">{{ closeStrategyReview?.market_state ?? '待生成' }}</strong></el-card></el-col>
                <el-col :md="4" :xs="12"><el-card shadow="never" class="metric-card"><span>多指数状态</span><strong class="review-metric"><el-tag :type="indexRegimeType">{{ indexRegimeLabel }}</el-tag></strong></el-card></el-col>
                <el-col :md="4" :xs="12"><el-card shadow="never" class="metric-card"><span>区间中位修复</span><strong>{{ closeIndexRegime?.median_range_retracement === undefined ? '-' : outcomePercent(closeIndexRegime.median_range_retracement) }}</strong></el-card></el-col>
                <el-col :md="4" :xs="12"><el-card shadow="never" class="metric-card"><span>概念成员覆盖</span><strong>{{ conceptBackfill.mapped_concepts }} / {{ conceptBackfill.total_concepts }}</strong></el-card></el-col>
                <el-col :md="4" :xs="12"><el-card shadow="never" class="metric-card"><span>本次回填完成</span><strong>{{ completedBackfillBoards }}</strong></el-card></el-col>
                <el-col :md="4" :xs="12"><el-card shadow="never" class="metric-card"><span>板块报告时间</span><strong class="review-metric">{{ closeBoardReport ? dateText(closeBoardReport.observed_at) : '待保存' }}</strong></el-card></el-col>
              </el-row>
              <el-card shadow="never" class="board-flow-card" v-loading="boardFlowLoading">
                <template #header><div class="card-header"><div><span>盘中板块资金分钟曲线</span><small class="realtime-refresh-time">每 60 秒自动增量刷新并检测轮动；红/绿突出当前净流入/净流出各前 10，其余板块仍完整显示。完整 Top10 快报另按五分钟节流。</small></div><el-space wrap><el-date-picker v-model="boardFlowDate" type="date" value-format="YYYY-MM-DD" size="small" class="board-flow-date" @change="resetBoardFlowCurves"/><el-radio-group v-model="boardFlowTaxonomy" size="small" @change="resetBoardFlowCurves"><el-radio-button value="industry">行业</el-radio-button><el-radio-button value="concept">概念</el-radio-button></el-radio-group><el-button :icon="Refresh" size="small" @click="loadBoardFlowCurves(false)">增量刷新</el-button></el-space></div></template>
                <el-alert v-if="boardFlowError" :title="`板块曲线读取失败：${boardFlowError}`" type="error" :closable="false" show-icon/>
                <template v-else>
                  <div class="board-flow-toolbar"><el-space wrap><el-tag type="info">{{ boardFlowSeriesRows.length }} 个板块</el-tag><el-tag type="success">{{ boardFlowWindowText }}</el-tag><el-tag :type="boardFlowLatestSnapshot?.coverage ? 'success' : 'warning'">最新覆盖 {{ boardFlowLatestSnapshot?.coverage ?? 0 }}</el-tag><el-tag type="info">最后真实点 {{ chinaDateTime(boardFlowLatestSnapshot?.observed_at) }}</el-tag><el-tag v-if="boardFlowGaps" type="warning">{{ boardFlowGaps }} 段缺口已补点</el-tag></el-space><el-select v-model="boardFlowFocus" multiple filterable clearable collapse-tags collapse-tags-tooltip placeholder="可选重点板块；留空显示全部" class="board-flow-focus"><el-option v-for="item in boardFlowSeriesRows" :key="`${item.taxonomy_key}:${item.sector_key}`" :label="item.label" :value="`${item.taxonomy_key}:${item.sector_key}`"/></el-select></div>
                  <el-empty v-if="!boardFlowSeriesRows.length" description="所选日期尚无板块分钟快照" :image-size="68"/>
                  <v-chart v-else :option="boardFlowChartOption" autoresize class="board-flow-chart"/>
                  <el-text type="info" class="review-note">{{ boardFlowNotice || '时间轴以 Asia/Shanghai 交易所时钟生成；缺失分钟沿用最近真实值，悬浮时会标记为补点。' }}</el-text>
                  <el-divider content-position="left">一分钟资金轮动事件</el-divider>
                  <el-alert title="每 60 秒比较相邻同源快照：同类板块资金变化进入前 5%、变化不少于 2 亿元且当前净流绝对值不少于 1 亿元时入队；流出转流入、流入转流出和单向急剧加速均需下一分钟方向保持，才推送飞书。" type="info" :closable="false" show-icon/>
                  <el-empty v-if="!boardRotationEvents.length" description="本交易日尚无满足阈值的板块轮动事件" :image-size="52" class="section-gap"/>
                  <el-table v-else :data="boardRotationEvents" max-height="300" size="small" class="section-gap">
                    <el-table-column label="观测时间" width="132"><template #default="{ row }">{{ chinaDateTime(row.last_observed_at) }}</template></el-table-column>
                    <el-table-column prop="label" label="板块" min-width="125" show-overflow-tooltip/>
                    <el-table-column label="轮动" width="105"><template #default="{ row }"><el-tag size="small" :type="row.direction === 'inflow' ? 'success' : 'danger'">{{ boardRotationKind(row) }}</el-tag></template></el-table-column>
                    <el-table-column label="一分钟净流变化" width="130"><template #default="{ row }">{{ Number(row.conditions?.previous_net_inflow ?? 0).toFixed(2) }} → {{ Number(row.conditions?.current_net_inflow ?? 0).toFixed(2) }} 亿</template></el-table-column>
                    <el-table-column label="状态" width="122"><template #default="{ row }"><el-tag size="small" :type="boardRotationStateType(row.state)">{{ boardRotationStateText(row.state) }}</el-tag></template></el-table-column>
                    <el-table-column label="飞书" width="112"><template #default="{ row }"><el-tag size="small" :type="row.delivery_status === 'sent' ? 'success' : row.delivery_status === 'failed' ? 'danger' : 'info'">{{ boardRotationDeliveryText(row) }}</el-tag></template></el-table-column>
                  </el-table>
                </template>
              </el-card>
              <el-card shadow="never" class="section-gap">
                <template #header><div class="card-header"><div><span>板块流向个股挖掘</span><small class="realtime-refresh-time">随五分钟板块快报自动更新；只使用精确成分映射完整且腾讯同刻报价齐全的板块。仅展示在研究台，不发送飞书。</small></div><el-tag :type="boardStockMining.run?.status === 'completed' ? 'success' : 'info'">{{ boardStockMining.run?.status ?? '等待快照' }}</el-tag></div></template>
                <el-alert :title="boardStockMining.notice ?? '研究候选由板块资金、个股主力流、量比、换手和价格结构共同筛出；不是买卖指令。'" type="info" :closable="false" show-icon/>
                <el-descriptions v-if="boardStockMining.run" :column="mobileLayout ? 1 : 4" border size="small" class="section-gap"><el-descriptions-item label="观测时间">{{ chinaDateTime(boardStockMining.run.observed_at) }}</el-descriptions-item><el-descriptions-item label="完整板块">{{ boardStockMining.run.coverage?.exact_complete_boards ?? 0 }}</el-descriptions-item><el-descriptions-item label="流入 / 流出">{{ boardStockMining.run.summary?.inflow_candidates ?? 0 }} / {{ boardStockMining.run.summary?.outflow_candidates ?? 0 }}</el-descriptions-item><el-descriptions-item label="跳过不完整映射">{{ boardStockMining.run.coverage?.partial_or_unmapped_boards_skipped ?? 0 }}</el-descriptions-item></el-descriptions>
                <el-tabs type="border-card" class="section-gap">
                  <el-tab-pane :label="`流入启动 ${boardStockMining.inflow?.length ?? 0}`"><el-empty v-if="!(boardStockMining.inflow?.length)" description="当前无满足严格活动门槛的流入候选" :image-size="52"/><el-table v-else :data="boardStockMining.inflow" max-height="320" size="small"><el-table-column prop="rank" label="#" width="44"/><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="88"/><el-table-column prop="label" label="精确板块" min-width="120"/><el-table-column prop="setup_key" label="结构" min-width="145"/><el-table-column label="板块净流" width="105"><template #default="{ row }">{{ displayValue(row.board_net_inflow) }}</template></el-table-column><el-table-column label="主力流" width="100"><template #default="{ row }">{{ displayValue(row.main_net_inflow) }}</template></el-table-column><el-table-column label="量比/换手" width="102"><template #default="{ row }">{{ displayValue(row.volume_ratio) }} / {{ displayValue(row.turnover_rate) }}%</template></el-table-column><el-table-column label="涨跌" width="72"><template #default="{ row }">{{ displayValue(row.pct_change) }}%</template></el-table-column><el-table-column prop="score" label="筛选分" width="80"/><el-table-column label="研究" width="72" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">分析</el-button></template></el-table-column></el-table></el-tab-pane>
                  <el-tab-pane :label="`流出风险 ${boardStockMining.outflow?.length ?? 0}`"><el-empty v-if="!(boardStockMining.outflow?.length)" description="当前无满足严格活动门槛的流出风险候选" :image-size="52"/><el-table v-else :data="boardStockMining.outflow" max-height="320" size="small"><el-table-column prop="rank" label="#" width="44"/><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="88"/><el-table-column prop="label" label="精确板块" min-width="120"/><el-table-column prop="setup_key" label="结构" min-width="145"/><el-table-column label="板块净流" width="105"><template #default="{ row }">{{ displayValue(row.board_net_inflow) }}</template></el-table-column><el-table-column label="主力流" width="100"><template #default="{ row }">{{ displayValue(row.main_net_inflow) }}</template></el-table-column><el-table-column label="量比/换手" width="102"><template #default="{ row }">{{ displayValue(row.volume_ratio) }} / {{ displayValue(row.turnover_rate) }}%</template></el-table-column><el-table-column label="涨跌" width="72"><template #default="{ row }">{{ displayValue(row.pct_change) }}%</template></el-table-column><el-table-column prop="score" label="风险分" width="80"/><el-table-column label="研究" width="72" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">分析</el-button></template></el-table-column></el-table></el-tab-pane>
                </el-tabs>
              </el-card>
              <el-card shadow="never" class="section-gap">
                <template #header><div class="card-header"><div><span>涨停关联股挖掘</span><small class="realtime-refresh-time">涨停事实 → 同花顺精确概念关联 → 腾讯同刻量价；已排除接近涨停的追板对象。仅展示在研究台，不发送飞书。</small></div><el-tag :type="limitLinkageMining.run?.status === 'completed' ? 'success' : 'info'">{{ limitLinkageMining.run?.status ?? '等待涨停池' }}</el-tag></div></template>
                <el-alert :title="limitLinkageMining.notice ?? '仅做板块内关联研究；必须再经分钟级承接确认，不能替代交易决策。'" type="warning" :closable="false" show-icon/>
                <el-descriptions v-if="limitLinkageMining.run" :column="mobileLayout ? 1 : 4" border size="small" class="section-gap"><el-descriptions-item label="观测时间">{{ chinaDateTime(limitLinkageMining.run.observed_at) }}</el-descriptions-item><el-descriptions-item label="涨停锚点">{{ limitLinkageMining.run.summary?.anchors ?? 0 }}</el-descriptions-item><el-descriptions-item label="精确关联">{{ limitLinkageMining.run.summary?.exact_relation_rows ?? 0 }}</el-descriptions-item><el-descriptions-item label="候选数">{{ limitLinkageMining.run.summary?.candidate_count ?? 0 }}</el-descriptions-item></el-descriptions>
                <el-empty v-if="!(limitLinkageMining.items?.length)" description="等待同一交易日涨停池与同刻行情同时满足严格条件" :image-size="52" class="section-gap"/>
                <el-table v-else :data="limitLinkageMining.items" max-height="320" size="small" class="section-gap"><el-table-column prop="rank" label="#" width="44"/><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="88"/><el-table-column label="涨停锚点" min-width="130"><template #default="{ row }">{{ (row.leader_names?.length ? row.leader_names : row.leader_symbols)?.join('、') ?? '-' }}</template></el-table-column><el-table-column label="共享概念" min-width="150"><template #default="{ row }">{{ row.shared_concepts }} · {{ row.concept_labels?.slice(0, 2).join('、') ?? '-' }}</template></el-table-column><el-table-column label="主力流" width="100"><template #default="{ row }">{{ displayValue(row.main_net_inflow) }}</template></el-table-column><el-table-column label="量比/换手" width="102"><template #default="{ row }">{{ displayValue(row.volume_ratio) }} / {{ displayValue(row.turnover_rate) }}%</template></el-table-column><el-table-column label="涨跌" width="72"><template #default="{ row }">{{ displayValue(row.pct_change) }}%</template></el-table-column><el-table-column prop="score" label="研究分" width="80"/><el-table-column label="研究" width="72" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">分析</el-button></template></el-table-column></el-table>
              </el-card>
              <el-card v-if="closeIndexRegime" shadow="never" header="多指数纠错反弹 / B浪情景核验">
                <el-alert :title="closeIndexRegime.interpretation ?? '多指数仅用于判断市场环境；B浪只是分析师情景标签，不直接触发个股买卖。'" :type="indexRegimeType" :closable="false" show-icon/>
                <el-table :data="closeIndexRegime.items ?? []" max-height="300" size="small" class="section-gap">
                  <el-table-column label="指数" min-width="105"><template #default="{ row }">{{ indexLabel(row.symbol) }}</template></el-table-column>
                  <el-table-column prop="trading_date" label="交易日" width="105"/>
                  <el-table-column prop="close" label="收盘" width="90"/>
                  <el-table-column label="区间回撤" width="100"><template #default="{ row }">{{ displayValue(row.drawdown_high_to_low_pct) }}%</template></el-table-column>
                  <el-table-column label="低点反弹" width="100"><template #default="{ row }">{{ displayValue(row.rebound_from_low_pct) }}%</template></el-table-column>
                  <el-table-column label="距高点" width="90"><template #default="{ row }">{{ displayValue(row.versus_period_high_pct) }}%</template></el-table-column>
                  <el-table-column label="区间修复" width="95"><template #default="{ row }">{{ outcomePercent(row.range_retracement) }}</template></el-table-column>
                  <el-table-column label="近5日" width="85"><template #default="{ row }">{{ displayValue(row.return_5_sessions_pct) }}%</template></el-table-column>
                  <el-table-column label="量能比" width="85"><template #default="{ row }">{{ displayValue(row.volume_ratio_5_vs_prior15) }}</template></el-table-column>
                </el-table>
              </el-card>
              <el-card shadow="never"><template #header><div class="card-header"><span>复盘证据与成员回填</span><el-space><el-button :loading="actionLoading === '补齐一批概念成员'" @click="advanceConceptBackfill">补齐一批成员</el-button><el-button type="primary" :loading="actionLoading === '保存收盘板块复盘'" @click="refreshCloseReview">保存/刷新复盘</el-button></el-space></div></template>
                <el-descriptions :column="mobileLayout ? 1 : 3" border size="small"><el-descriptions-item label="资金流交易日">{{ conceptBackfill.trade_date ?? '-' }}</el-descriptions-item><el-descriptions-item label="自动回填">{{ conceptBackfill.automatic?.enabled ? `开启（每批 ${conceptBackfill.automatic.batch_size}）` : '关闭' }}</el-descriptions-item><el-descriptions-item label="回填状态"><el-space wrap><el-tag v-for="item in conceptBackfill.states" :key="item.state" :type="item.state === 'completed' ? 'success' : item.state === 'failed' ? 'danger' : 'info'">{{ item.state }} {{ item.boards }}</el-tag></el-space></el-descriptions-item></el-descriptions>
                <el-text type="info" class="section-gap review-note">{{ conceptBackfill.notice }}</el-text>
              </el-card>
              <el-card shadow="never"><template #header><div class="card-header"><span>盘后蓄势与首动候选池</span><el-space><el-tag :type="postCloseStrategyRun?.status === 'completed' ? 'success' : postCloseStrategyRun?.status === 'blocked' ? 'warning' : 'info'">{{ postCloseStrategyRun?.status ?? '未运行' }}</el-tag><el-button type="primary" :loading="actionLoading === '运行盘后蓄势与首动筛选'" @click="runPostCloseStrategy">运行盘后策略</el-button></el-space></div></template>
                <el-alert title="自动任务每天上海时间 18:55--19:10 在已落库日线完整时运行；30 日蓄势是严格结构，15 日结果仅用于历史积累期的观察，不自动加入观察池或下单。" type="info" :closable="false" show-icon/>
                <el-descriptions v-if="postCloseStrategyRun" :column="mobileLayout ? 1 : 4" border size="small" class="section-gap"><el-descriptions-item label="交易日">{{ postCloseStrategyRun.as_of_date ?? '-' }}</el-descriptions-item><el-descriptions-item label="模型">{{ postCloseStrategyRun.model_version ?? '-' }}</el-descriptions-item><el-descriptions-item label="日线覆盖">{{ displayValue(postCloseStrategyRun.source_status?.daily_symbols) }}</el-descriptions-item><el-descriptions-item label="返回候选">{{ displayValue(postCloseStrategyRun.summary?.returned) }}</el-descriptions-item></el-descriptions>
                <el-empty v-if="postCloseStrategyRun && !postCloseCandidates.length" description="当前没有满足严格门槛的候选，或日线历史仍不足" :image-size="58" class="section-gap"/>
                <el-table v-else :data="postCloseCandidates" max-height="420" size="small" class="section-gap"><el-table-column prop="rank" label="#" width="48"/><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="95"/><el-table-column label="类型" width="118"><template #default="{ row }"><el-tag size="small" :type="postCloseCandidateType(row.candidate_type)">{{ postCloseCandidateLabel(row.candidate_type) }}</el-tag></template></el-table-column><el-table-column prop="score" label="分数" width="75"/><el-table-column label="收盘结构" min-width="220"><template #default="{ row }">{{ row.structure?.notice ?? row.structure?.status ?? '-' }}</template></el-table-column><el-table-column label="精确概念" min-width="125"><template #default="{ row }">{{ row.board_context?.exact_member_mapping ? row.board_context?.label ?? '已映射' : '无精确映射' }}</template></el-table-column><el-table-column label="板块流" width="95"><template #default="{ row }">{{ displayValue(row.board_context?.net_amount) }}</template></el-table-column><el-table-column label="风险" min-width="190"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.risk_flags ?? []" :key="flag" size="small" type="warning">{{ flag }}</el-tag></el-space></template></el-table-column><el-table-column label="研究" width="86" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">技术分析</el-button></template></el-table-column></el-table>
              </el-card>
              <el-card shadow="never"><template #header><div class="card-header"><span>龙头/连板/首板分钟拉升形态</span><el-space><el-tag :type="strategyPatternRun?.status === 'completed' ? 'success' : strategyPatternRun?.status === 'partial' ? 'warning' : 'info'">{{ strategyPatternRun?.status ?? '未运行' }}</el-tag><el-button type="primary" :loading="actionLoading === '挖掘涨停拉升形态'" @click="runStrategyPatternMining">运行形态挖掘</el-button></el-space></div></template>
                <el-alert title="按同花顺涨停池和连板梯队分层，连接精确概念资金流、Super 日K及腾讯分钟量价。地天板只在深跌反抽、收复昨收、承接三个阶段分别标注，不直接给出买入指令。" type="warning" :closable="false" show-icon/>
                <el-alert v-if="strategyPoolCoverage.notice" :title="strategyPoolCoverage.notice" :type="strategyPoolCoverage.status === 'two_source_union' ? 'success' : 'warning'" :closable="false" show-icon class="section-gap"/>
                <el-descriptions v-if="strategyPatternRun" :column="mobileLayout ? 1 : 6" border size="small" class="section-gap"><el-descriptions-item label="交易日">{{ strategyPatternRun.as_of_date ?? '-' }}</el-descriptions-item><el-descriptions-item label="模型">{{ strategyPatternRun.model_version ?? '-' }}</el-descriptions-item><el-descriptions-item label="并集涨停池">{{ strategyPoolCoverage.union_count ?? strategyLimitPool.length }} 只</el-descriptions-item><el-descriptions-item label="同花顺/东财/交集">{{ strategyPoolCoverage.tushare_count ?? '-' }} / {{ strategyPoolCoverage.eastmoney_count ?? '-' }} / {{ strategyPoolCoverage.intersection_count ?? '-' }}</el-descriptions-item><el-descriptions-item label="多板并集/梯级直出">{{ strategyPoolCoverage.multi_board_union_count ?? strategyLimitLadder.length }} / {{ strategyPoolCoverage.limit_step_count ?? '-' }}</el-descriptions-item><el-descriptions-item label="精选/回放完成">{{ strategyPatternPicks.length }} / {{ displayValue(strategyPatternRun.summary?.minute_completed) }}</el-descriptions-item></el-descriptions>
                <el-tabs type="border-card" class="section-gap">
                  <el-tab-pane :label="`涨停池 ${strategyLimitPool.length}`">
                    <el-table :data="strategyLimitPool" max-height="500" size="small"><el-table-column prop="rank" label="#" width="46"/><el-table-column prop="ts_code" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="92"/><el-table-column prop="tag" label="梯队" width="90"/><el-table-column label="来源" width="96"><template #default="{ row }"><el-tag size="small" :type="(row.sources?.length ?? 0) >= 2 ? 'success' : 'warning'">{{ (row.sources?.length ?? 0) >= 2 ? '双源' : '单源' }}</el-tag></template></el-table-column><el-table-column label="状态" width="82"><template #default="{ row }"><el-tag size="small" :type="row.status === '一字板' ? 'danger' : 'warning'">{{ row.status ?? '-' }}</el-tag></template></el-table-column><el-table-column label="涨幅" width="78"><template #default="{ row }">{{ displayValue(row.pct_chg) }}%</template></el-table-column><el-table-column label="换手" width="78"><template #default="{ row }">{{ displayValue(row.turnover_rate) }}%</template></el-table-column><el-table-column label="5日量" width="76"><template #default="{ row }">{{ displayValue(row.volume_multiple_5d) }}</template></el-table-column><el-table-column label="精确板块/净流" min-width="145"><template #default="{ row }">{{ row.board_context?.label ?? '-' }} / {{ displayValue(row.board_context?.net_amount) }}</template></el-table-column><el-table-column label="龙虎榜机构净额" width="120"><template #default="{ row }">{{ moneyWan(row.lhb_context?.institution_net_buy) }}</template></el-table-column><el-table-column prop="open_num" label="开板" width="68"/><el-table-column prop="limit_amount" label="封单金额" width="110"/><el-table-column label="封板率" width="82"><template #default="{ row }">{{ outcomePercent(row.limit_up_suc_rate) }}</template></el-table-column><el-table-column prop="lu_desc" label="涨停原因" min-width="250" show-overflow-tooltip/><el-table-column label="研究" width="86" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.ts_code)">技术分析</el-button></template></el-table-column></el-table>
                  </el-tab-pane>
                  <el-tab-pane :label="`连板/多板 ${strategyLimitLadder.length}`">
                    <el-table :data="strategyLimitLadder" max-height="500" size="small"><el-table-column prop="rank" label="#" width="46"/><el-table-column prop="ts_code" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="100"/><el-table-column label="板数" width="82"><template #default="{ row }"><el-tag type="danger" size="small">{{ row.nums }} 板</el-tag></template></el-table-column><el-table-column prop="tag" label="区间梯队" width="95"/><el-table-column label="梯级来源" width="100"><template #default="{ row }"><el-tag size="small" :type="row.ladder_sources?.includes('tushare_limit_step') ? 'success' : 'info'">{{ row.ladder_sources?.includes('tushare_limit_step') ? '梯级直出' : '多板补齐' }}</el-tag></template></el-table-column><el-table-column prop="status" label="封板状态" width="90"/><el-table-column label="涨幅" width="78"><template #default="{ row }">{{ displayValue(row.pct_chg) }}%</template></el-table-column><el-table-column label="换手" width="78"><template #default="{ row }">{{ displayValue(row.turnover_rate) }}%</template></el-table-column><el-table-column label="5日量" width="76"><template #default="{ row }">{{ displayValue(row.volume_multiple_5d) }}</template></el-table-column><el-table-column label="板块/净流" min-width="135"><template #default="{ row }">{{ row.board_context?.label ?? '-' }} / {{ displayValue(row.board_context?.net_amount) }}</template></el-table-column><el-table-column label="龙虎榜机构净额" width="120"><template #default="{ row }">{{ moneyWan(row.lhb_context?.institution_net_buy) }}</template></el-table-column><el-table-column prop="open_num" label="开板" width="68"/><el-table-column prop="limit_amount" label="封单金额" width="110"/><el-table-column prop="lu_desc" label="涨停原因" min-width="250" show-overflow-tooltip/><el-table-column label="研究" width="86" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.ts_code)">技术分析</el-button></template></el-table-column></el-table>
                  </el-tab-pane>
                  <el-tab-pane :label="`今日精选 ${strategyPatternPicks.length}`">
                    <el-table :data="strategyPatternPicks" max-height="500" size="small"><el-table-column prop="rank" label="#" width="46"/><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="92"/><el-table-column label="复盘评分" width="86"><template #default="{ row }"><el-tag :type="row.limit_context?.review_tier === 'priority_review' ? 'success' : 'warning'">{{ displayValue(row.limit_context?.review_score) }}</el-tag></template></el-table-column><el-table-column label="层级" width="92"><template #default="{ row }">{{ reviewTierText(row.limit_context?.review_tier) }}</template></el-table-column><el-table-column label="梯队" width="90"><template #default="{ row }">{{ row.limit_context?.tag ?? '-' }}</template></el-table-column><el-table-column label="板块/净流" min-width="135"><template #default="{ row }">{{ row.board_context?.label ?? '-' }} / {{ displayValue(row.board_context?.net_amount) }}</template></el-table-column><el-table-column label="龙虎榜机构净额" width="120"><template #default="{ row }">{{ moneyWan(row.limit_context?.lhb_context?.institution_net_buy) }}</template></el-table-column><el-table-column label="5日量" width="76"><template #default="{ row }">{{ displayValue(row.daily_features?.volume_multiple_5d) }}</template></el-table-column><el-table-column label="入选依据" min-width="260"><template #default="{ row }">{{ row.limit_context?.selection_reasons?.join('；') || '-' }}</template></el-table-column><el-table-column label="风险" min-width="180"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.risk_flags ?? []" :key="flag" type="warning" size="small">{{ flag }}</el-tag></el-space></template></el-table-column><el-table-column label="研究" width="86" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">技术分析</el-button></template></el-table-column></el-table>
                  </el-tab-pane>
                  <el-tab-pane :label="`形态样本 ${strategyPatternSamples.length}`">
                    <el-table :data="strategyPatternSamples" max-height="500" size="small"><el-table-column prop="rank" label="#" width="46"/><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="name" label="名称" min-width="92"/><el-table-column label="主样本" width="105"><template #default="{ row }"><el-tag size="small" :type="row.primary_cohort === 'ground_to_sky' ? 'danger' : row.primary_cohort === 'consecutive_limit' ? 'warning' : 'info'">{{ patternCohortLabel(row.primary_cohort) }}</el-tag></template></el-table-column><el-table-column label="梯队/盘前→盘后位次" width="160"><template #default="{ row }">{{ row.limit_context?.tag ?? '-' }} / {{ row.limit_context?.preopen_limit_pool_rank ?? '-' }} → {{ row.limit_context?.limit_pool_market_rank ?? '-' }}</template></el-table-column><el-table-column label="精确板块" min-width="115"><template #default="{ row }">{{ row.board_context?.label ?? '未映射' }}</template></el-table-column><el-table-column label="低点→收盘" width="125"><template #default="{ row }">{{ displayValue(row.daily_features?.low_pct) }}% → {{ displayValue(row.daily_features?.close_pct) }}%</template></el-table-column><el-table-column label="5日量能倍数" width="105"><template #default="{ row }">{{ displayValue(row.daily_features?.volume_multiple_5d) }}</template></el-table-column><el-table-column label="首次异动" width="82"><template #default="{ row }">{{ row.intraday_pattern?.deep_reversal_impulse?.time ?? row.intraday_pattern?.deep_discount_stabilization?.time ?? row.intraday_pattern?.standard_ignition?.time ?? row.intraday_pattern?.opening_drive?.first_four_pct_time ?? '-' }}</template></el-table-column><el-table-column label="收复昨收/承接" width="125"><template #default="{ row }">{{ row.intraday_pattern?.previous_close_reclaim?.time ?? '-' }} / {{ row.intraday_pattern?.previous_close_acceptance?.time ?? '-' }}</template></el-table-column><el-table-column label="触板" width="72"><template #default="{ row }">{{ row.intraday_pattern?.limit_reclaim?.time ?? '-' }}</template></el-table-column><el-table-column label="形态" min-width="185"><template #default="{ row }"><el-space wrap><el-tag v-for="tag in row.intraday_pattern?.pattern_tags ?? []" :key="tag" size="small" type="info">{{ tag }}</el-tag></el-space></template></el-table-column><el-table-column label="研究" width="86" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="studyConceptCandidate(row.symbol)">技术分析</el-button></template></el-table-column></el-table>
                  </el-tab-pane>
                </el-tabs>
              </el-card>
              <el-row :gutter="14"><el-col :md="14" :xs="24"><el-card shadow="never" header="同花顺概念资金流与精确成分覆盖"><el-table :data="reviewConceptBoards" max-height="500" size="small" @row-click="(row: BoardItem) => selectedReviewBoardKey = row.sector_key"><el-table-column prop="label" label="概念" min-width="140"/><el-table-column prop="net_inflow" label="净流入" width="105"/><el-table-column label="涨跌幅" width="84"><template #default="{ row }">{{ displayValue(row.change_pct) }}%</template></el-table-column><el-table-column prop="mapped_members" label="精确成员" width="92"/><el-table-column prop="quoted_members" label="腾讯匹配" width="92"/><el-table-column label="Top10" width="78"><template #default="{ row }"><el-tag size="small" :type="row.top_stocks.length >= 10 ? 'success' : 'warning'">{{ row.top_stocks.length }} 只</el-tag></template></el-table-column></el-table></el-card></el-col><el-col :md="10" :xs="24"><el-card shadow="never"><template #header><div class="card-header"><span>板块个股 Top10</span><el-select v-model="selectedReviewBoardKey" size="small" class="review-board-select"><el-option v-for="board in reviewConceptBoards" :key="board.sector_key" :label="board.label" :value="board.sector_key"/></el-select></div></template><el-empty v-if="!selectedReviewBoard" description="等待可复盘的精确成员映射" :image-size="58"/><template v-else><el-descriptions :column="2" size="small" border><el-descriptions-item label="概念">{{ selectedReviewBoard.label }}</el-descriptions-item><el-descriptions-item label="净流入">{{ displayValue(selectedReviewBoard.net_inflow) }}</el-descriptions-item></el-descriptions><el-table :data="selectedReviewBoardStocks" max-height="390" size="small" class="section-gap"><el-table-column prop="symbol" label="代码" width="96"/><el-table-column prop="name" label="名称" min-width="90"/><el-table-column prop="main_net_inflow" label="主力流" width="94"/><el-table-column prop="volume_ratio" label="量比" width="68"/><el-table-column prop="turnover_rate" label="换手" width="68"/><el-table-column label="涨跌" width="68"><template #default="{ row }">{{ displayValue(row.pct_change) }}%</template></el-table-column></el-table></template></el-card></el-col></el-row>
              <el-card shadow="never"><template #header><div class="card-header"><span>盘中信号结果与归因</span><el-button :loading="actionLoading === '结算盘中信号'" @click="settleIntradayOutcomes">结算已确认信号</el-button></div></template>
                <el-alert title="只结算 confirmed / alerted 信号。收益是信号后的可观察价格路径，不是成交回报；pending 表示后续报价或下一交易日收盘尚未到达。" type="info" :closable="false" show-icon/>
                <el-table :data="intradayOutcomeSummary" max-height="220" size="small" class="section-gap"><el-table-column prop="horizon_key" label="期限" width="105"/><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="outcomeStatusType(row.status)">{{ row.status }}</el-tag></template></el-table-column><el-table-column prop="rows" label="样本" width="86"/><el-table-column label="平均方向收益" width="135"><template #default="{ row }">{{ outcomePercent(row.avg_directional_return) }}</template></el-table-column><el-table-column label="平均 MFE" width="110"><template #default="{ row }">{{ outcomePercent(row.avg_mfe) }}</template></el-table-column><el-table-column label="平均 MAE" width="110"><template #default="{ row }">{{ outcomePercent(row.avg_mae) }}</template></el-table-column></el-table>
                <el-divider content-position="left">点时分层归因</el-divider>
                <el-alert title="分层统计按信号触发时已经可得的市场状态、板块/同伴联动、信号阶段和模型版本计算；不足 30 个成熟样本的分组只作描述，不用于调参。" type="warning" :closable="false" show-icon/>
                <el-descriptions :column="mobileLayout ? 1 : 3" border size="small" class="section-gap">
                  <el-descriptions-item label="正式验证门禁"><el-tag :type="attributionValidationGate.status === 'ready_for_formal_validation' ? 'success' : 'warning'">{{ attributionValidationGate.status === 'ready_for_formal_validation' ? '可进入正式验证' : '积累中' }}</el-tag></el-descriptions-item>
                  <el-descriptions-item label="独立成熟信号">{{ attributionValidationGate.matured_unique_signals }} / {{ attributionValidationGate.required_unique_signals }}</el-descriptions-item>
                  <el-descriptions-item label="覆盖交易日">{{ attributionValidationGate.trading_days }} / {{ attributionValidationGate.required_trading_days }}</el-descriptions-item>
                </el-descriptions>
                <el-table :data="intradayAttributionSummary" max-height="380" size="small" class="section-gap">
                  <el-table-column label="维度" width="100"><template #default="{ row }">{{ attributionDimensionLabel(row.dimension) }}</template></el-table-column>
                  <el-table-column label="分组" min-width="165"><template #default="{ row }">{{ attributionCohortLabel(row.cohort) }}</template></el-table-column>
                  <el-table-column prop="horizon_key" label="期限" width="72"/>
                  <el-table-column label="成熟/全部" width="92"><template #default="{ row }">{{ row.matured }} / {{ row.rows }}</template></el-table-column>
                  <el-table-column label="命中率" width="88"><template #default="{ row }">{{ outcomePercent(row.hit_rate) }}</template></el-table-column>
                  <el-table-column label="平均方向收益" width="118"><template #default="{ row }">{{ outcomePercent(row.avg_directional_return) }}</template></el-table-column>
                  <el-table-column label="MFE / MAE" width="145"><template #default="{ row }">{{ outcomePercent(row.avg_mfe) }} / {{ outcomePercent(row.avg_mae) }}</template></el-table-column>
                  <el-table-column label="盈亏比" width="78"><template #default="{ row }">{{ row.payoff_ratio === null || row.payoff_ratio === undefined ? '-' : Number(row.payoff_ratio).toFixed(2) }}</template></el-table-column>
                  <el-table-column label="样本门禁" width="108"><template #default="{ row }"><el-tag size="small" :type="attributionStatusType(row.evaluation_status)">{{ row.evaluation_status === 'cohort_reviewable' ? '可复核' : '仅描述' }}</el-tag></template></el-table-column>
                </el-table>
                <el-table :data="intradayOutcomes" max-height="360" size="small"><el-table-column prop="symbol" label="代码" width="104"/><el-table-column prop="signal_type" label="信号" width="85"/><el-table-column prop="horizon_key" label="期限" width="70"/><el-table-column label="触发时间" width="160"><template #default="{ row }">{{ dateText(row.observed_at) }}</template></el-table-column><el-table-column label="状态" width="96"><template #default="{ row }"><el-tag :type="outcomeStatusType(row.status)">{{ row.status }}</el-tag></template></el-table-column><el-table-column label="方向收益" width="115"><template #default="{ row }">{{ outcomePercent(row.raw_return) }}</template></el-table-column><el-table-column label="MFE / MAE" width="150"><template #default="{ row }">{{ outcomePercent(row.maximum_favorable_excursion) }} / {{ outcomePercent(row.maximum_adverse_excursion) }}</template></el-table-column><el-table-column prop="tradability" label="测量方式" min-width="150" show-overflow-tooltip/></el-table>
              </el-card>
              <el-card shadow="never"><template #header><div class="card-header"><span>分析师成绩单门禁</span><el-button :loading="actionLoading === '刷新分析师成绩单'" @click="recomputeAnalystScorecards">刷新成绩单</el-button></div></template>
                <el-alert title="分析师文本不会因篇数多而获得更高权重。只有方向明确、标的可映射且未来路径已结算的股票观点，才可累积成绩单。" type="warning" :closable="false" show-icon/>
                <el-table :data="analystReadiness" max-height="260" size="small" class="section-gap"><el-table-column prop="name" label="分析师" min-width="110"/><el-table-column prop="stock_claims" label="股票观点" width="95"/><el-table-column prop="directional_stock_claims" label="方向明确" width="95"/><el-table-column prop="settled_stock_outcomes" label="已结算" width="90"/><el-table-column label="可入权重" width="90"><template #default="{ row }"><el-tag :type="row.mature ? 'success' : 'warning'">{{ row.mature ? '可复核' : '未成熟' }}</el-tag></template></el-table-column><el-table-column label="门禁原因" min-width="210"><template #default="{ row }">{{ analystReadinessText(row.reason) }}</template></el-table-column></el-table>
                <el-table v-if="analystScorecards.length" :data="analystScorecards" max-height="240" size="small"><el-table-column prop="analyst_id" label="分析师"/><el-table-column prop="horizon_days" label="周期" width="75"/><el-table-column prop="observations" label="样本" width="80"/><el-table-column label="命中率" width="95"><template #default="{ row }">{{ outcomePercent(row.hit_rate) }}</template></el-table-column><el-table-column label="方向收益" width="115"><template #default="{ row }">{{ outcomePercent(row.mean_directional_return) }}</template></el-table-column><el-table-column label="超额收益" width="115"><template #default="{ row }">{{ outcomePercent(row.mean_excess_return) }}</template></el-table-column></el-table>
              </el-card>
              <el-card shadow="never" header="龙虎榜：下一交易日观察背景"><el-alert title="仅展示已入库的龙虎榜事件；用于复盘“谁在何处出现异常交易”，不倒灌为当日盘中信号。" type="warning" :closable="false" show-icon/><el-table :data="lhbEvents" max-height="360" size="small" class="section-gap"><el-table-column prop="symbol" label="代码" width="105"/><el-table-column prop="occurred_at" label="披露时间" width="165"><template #default="{ row }">{{ dateText(row.occurred_at) }}</template></el-table-column><el-table-column prop="title" label="龙虎榜事件" min-width="330" show-overflow-tooltip/><el-table-column prop="source" label="来源" width="110"/><el-table-column label="原始公告" width="88"><template #default="{ row }"><el-link v-if="row.url" :href="row.url" target="_blank" type="primary">查看</el-link><span v-else>-</span></template></el-table-column></el-table></el-card>
            </el-tab-pane>
            <el-tab-pane label="策略与股票池" name="strategy">
              <el-row :gutter="14">
                <el-col :md="9" :xs="24"><el-card shadow="never" header="核心股票池"><el-form label-position="top"><el-form-item label="股票代码"><el-input v-model="universeText" type="textarea" :rows="4" placeholder="000636.SZ, 603580.SH"/></el-form-item><el-form-item label="优先级"><el-input-number v-model="universePriority" :min="1" :max="10000"/></el-form-item><el-button type="primary" :loading="actionLoading === '更新核心股票池'" @click="saveUniverse">保存股票池</el-button></el-form><el-table :data="universe" size="small" max-height="250" class="section-gap"><el-table-column prop="symbol" label="代码"/><el-table-column prop="name" label="名称"/><el-table-column prop="priority" label="优先级" width="82"/></el-table></el-card></el-col>
                <el-col :md="15" :xs="24"><el-card shadow="never"><template #header><div class="card-header"><span>方向推荐</span><el-space><el-button :loading="actionLoading === '构建多源特征'" @click="runAction('构建多源特征','/api/research/features/build',{ universe_key: 'core' })">构建特征</el-button><el-button type="primary" :loading="actionLoading === '生成方向推荐'" @click="runAction('生成方向推荐','/api/research/recommendations/generate',{ universe_key: 'core', horizon_days: 20 })">生成推荐</el-button></el-space></div></template><el-alert title="推荐基于已落库的多源证据、技术趋势、资金流与已审核分析师观点；仅供研究，不自动下单。" type="info" :closable="false" show-icon/><el-table :data="recommendations" max-height="420" class="section-gap"><el-table-column prop="rank" label="#" width="52"/><el-table-column prop="symbol" label="标的"/><el-table-column label="方向" width="82"><template #default="{ row }"><el-tag :type="recommendationType(row.direction)">{{ recommendationDirection(row.direction) }}</el-tag></template></el-table-column><el-table-column prop="score" label="评分" width="75"/><el-table-column label="置信度" width="92"><template #default="{ row }">{{ row.confidence === undefined ? '-' : `${Math.round(row.confidence * 100)}%` }}</template></el-table-column><el-table-column prop="horizon_days" label="周期" width="72"/><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="row.decision === 'research_candidate' ? 'success' : row.decision === 'no_trade' ? 'danger' : 'info'">{{ row.decision }}</el-tag></template></el-table-column><el-table-column label="风险" min-width="160"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.risk_flags ?? []" :key="flag" size="small" type="warning">{{ flag }}</el-tag></el-space></template></el-table-column></el-table></el-card></el-col>
              </el-row>
              <el-card shadow="never" header="特征证据"><el-table :data="featureItems" max-height="360"><el-table-column prop="symbol" label="标的" width="120"/><el-table-column prop="name" label="名称" width="120"/><el-table-column label="收盘" width="95"><template #default="{ row }">{{ displayValue(row.features.close) }}</template></el-table-column><el-table-column label="5日收益" width="110"><template #default="{ row }">{{ displayValue(row.features.return_5) }}</template></el-table-column><el-table-column label="20日收益" width="110"><template #default="{ row }">{{ displayValue(row.features.return_20) }}</template></el-table-column><el-table-column label="东财主力占比" width="130"><template #default="{ row }">{{ displayValue(featureRecord(row,'moneyflow_dc').net_amount_rate) }}</template></el-table-column><el-table-column label="分析师共识" width="125"><template #default="{ row }">{{ displayValue(featureRecord(row,'analyst').consensus) }}</template></el-table-column><el-table-column label="质量标记" min-width="180"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.quality_flags" :key="flag" size="small" type="warning">{{ flag }}</el-tag></el-space></template></el-table-column></el-table></el-card>
            </el-tab-pane>
            <el-tab-pane label="因子与回测" name="factor-lab">
              <el-alert title="因子评估和回测只读取已入库的点时行情；样本不足时会明确标记，不会提升为推荐信号。" type="info" :closable="false" show-icon />
              <el-card shadow="never"><template #header><div class="card-header"><span>因子实验控制台</span><el-space><el-select v-model="factorHorizon" class="factor-horizon"><el-option :value="1" label="1日收益"/><el-option :value="5" label="5日收益"/><el-option :value="20" label="20日收益"/></el-select><el-button type="primary" :loading="actionLoading === '评估因子'" @click="runFactorEvaluation">评估所选因子</el-button></el-space></div></template><el-checkbox-group v-model="selectedFactors" class="factor-picker"><el-checkbox v-for="factor in factors" :key="factor.factor_key" :value="factor.factor_key">{{ factor.label }}</el-checkbox></el-checkbox-group></el-card>
              <el-row :gutter="14"><el-col :md="13" :xs="24"><el-card shadow="never" header="Rank IC 对比"><VChart class="research-chart" :option="factorChartOption" autoresize/><el-empty v-if="!factorEvaluations.length" description="尚未运行因子评估" :image-size="68"/></el-card></el-col><el-col :md="11" :xs="24"><el-card shadow="never" header="因子注册表"><el-table :data="factors" max-height="330" size="small"><el-table-column prop="label" label="因子"/><el-table-column prop="category" label="类别" width="95"/><el-table-column prop="version" label="版本" width="90"/><el-table-column label="框架" min-width="145"><template #default="{ row }"><el-space wrap><el-tag v-for="tag in row.framework_tags" :key="tag" size="small" type="info">{{ tag }}</el-tag></el-space></template></el-table-column></el-table></el-card></el-col></el-row>
              <el-card shadow="never" header="因子评估结果"><el-table :data="factorEvaluations" max-height="360"><el-table-column prop="label" label="因子"/><el-table-column label="状态" width="130"><template #default="{ row }"><el-tag :type="row.status === 'completed' ? 'success' : 'warning'">{{ row.status }}</el-tag></template></el-table-column><el-table-column prop="horizon_days" label="周期" width="70"/><el-table-column prop="observations" label="样本" width="80"/><el-table-column prop="cross_section_days" label="截面日" width="90"/><el-table-column label="平均 Rank IC" width="120"><template #default="{ row }">{{ metricNumber(row.metrics,'rank_ic_mean') }}</template></el-table-column><el-table-column label="ICIR" width="95"><template #default="{ row }">{{ metricNumber(row.metrics,'rank_icir') }}</template></el-table-column><el-table-column label="多空分层" width="110"><template #default="{ row }">{{ metricNumber(row.metrics,'top_minus_bottom_return') }}</template></el-table-column><el-table-column label="顶部换手" width="110"><template #default="{ row }">{{ metricNumber(row.metrics,'top_bucket_turnover') }}</template></el-table-column></el-table></el-card>
              <el-row :gutter="14"><el-col :md="8" :xs="24"><el-card shadow="never" header="A股约束回测"><el-form label-position="top"><el-form-item label="调仓间隔"><el-input-number v-model="backtestForm.rebalance_days" :min="1" :max="60"/></el-form-item><el-form-item label="持有天数"><el-input-number v-model="backtestForm.hold_days" :min="1" :max="60"/></el-form-item><el-form-item label="最多持仓"><el-input-number v-model="backtestForm.top_n" :min="1" :max="500"/></el-form-item><el-form-item label="单边成本 bps"><el-input-number v-model="backtestForm.total_cost_bps" :min="0" :max="500"/></el-form-item><el-button type="primary" :loading="actionLoading === '运行A股约束回测'" @click="runStrategyBacktest">运行回测</el-button></el-form></el-card></el-col><el-col :md="16" :xs="24"><el-card shadow="never" header="最新策略净值"><template v-if="latestExperiment"><el-row :gutter="12" class="metric-row"><el-col :span="6"><el-statistic title="总收益" :value="Number(latestExperiment.metrics.total_return || 0) * 100" suffix="%" :precision="2"/></el-col><el-col :span="6"><el-statistic title="年化收益" :value="Number(latestExperiment.metrics.annualized_return || 0) * 100" suffix="%" :precision="2"/></el-col><el-col :span="6"><el-statistic title="最大回撤" :value="Number(latestExperiment.metrics.max_drawdown || 0) * 100" suffix="%" :precision="2"/></el-col><el-col :span="6"><el-statistic title="交易笔数" :value="Number(latestExperiment.metrics.trades || 0)"/></el-col></el-row><VChart class="research-chart" :option="equityChartOption" autoresize/></template><el-empty v-else description="尚未运行策略实验" :image-size="68"/></el-card></el-col></el-row>
              <el-card shadow="never" header="开源框架与训练路线"><el-table :data="frameworks" max-height="280" size="small"><el-table-column prop="label" label="框架"/><el-table-column prop="role" label="用途"/><el-table-column prop="integration_mode" label="接入方式"/><el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.status === 'native' ? 'success' : row.status === 'planned' ? 'info' : 'warning'">{{ row.status }}</el-tag></template></el-table-column><el-table-column prop="license_note" label="许可" width="180"/></el-table><el-divider content-position="left">H100 训练门槛</el-divider><el-timeline><el-timeline-item v-for="stage in trainingRoadmap.stages" :key="stage.stage" :timestamp="stage.compute"><strong>{{ stage.stage }}</strong><div>{{ stage.gate }}</div></el-timeline-item></el-timeline><el-text type="info">{{ trainingRoadmap.policy }}</el-text></el-card>
            </el-tab-pane>
            <el-tab-pane label="个股研究" name="stock-study">
              <el-card shadow="never" header="股票研究">
                <el-form inline class="study-form" @submit.prevent="runStockStudy">
                  <el-form-item label="股票代码"><el-input v-model="studySymbol" placeholder="000636.SZ" clearable /></el-form-item>
                  <el-form-item label="日线窗口"><el-input-number v-model="studyLookback" :min="5" :max="45" /></el-form-item>
                  <el-form-item><el-space><el-button type="primary" :loading="studyLoading" @click="runStockStudy">刷新数据并分析</el-button><el-button :loading="actionLoading === '验证双源实时接口'" @click="probeRealtimeMinutes">验证实时接口</el-button><el-button :loading="actionLoading === 'AkShare补充探测'" @click="probeAkshareSupplement">AkShare完整补充</el-button><el-button :loading="actionLoading === 'AkShare宏观跨资产补充'" @click="probeAkshareMacroSupplement">宏观跨资产</el-button></el-space></el-form-item>
                </el-form>
                <el-alert v-if="studyError" :title="studyError" type="error" :closable="false" show-icon />
              </el-card>
              <template v-if="stockStudy">
                <el-alert :title="stockStudy.combined.notice" type="info" :closable="false" show-icon class="section-gap" />
                <el-row :gutter="14">
                  <el-col :md="12" :xs="24"><el-card shadow="never" header="综合研究"><el-descriptions :column="1" border><el-descriptions-item label="标的">{{ stockStudy.symbol }}</el-descriptions-item><el-descriptions-item label="截至">{{ stockStudy.as_of_date }}</el-descriptions-item><el-descriptions-item label="综合评分"><el-progress :percentage="stockStudy.combined.score" :stroke-width="12" /></el-descriptions-item><el-descriptions-item label="结论"><el-tag :type="studyType(stockStudy.combined.stance)">{{ studyStance(stockStudy.combined.stance) }}</el-tag></el-descriptions-item></el-descriptions><el-divider content-position="left">依据</el-divider><el-space wrap><el-tag v-for="reason in stockStudy.combined.reasons" :key="reason" type="info">{{ reason }}</el-tag></el-space></el-card></el-col>
                  <el-col :md="12" :xs="24"><el-card shadow="never" header="技术面"><el-descriptions :column="2" border><el-descriptions-item label="趋势"><el-tag :type="stockStudy.technical.trend === 'positive' ? 'success' : stockStudy.technical.trend === 'negative' ? 'danger' : 'info'">{{ stockStudy.technical.trend }}</el-tag></el-descriptions-item><el-descriptions-item label="评分">{{ displayValue(stockStudy.technical.score) }}</el-descriptions-item><el-descriptions-item label="收盘">{{ displayValue(stockStudy.technical.close) }}</el-descriptions-item><el-descriptions-item label="5日涨跌">{{ displayValue(stockStudy.technical.return_5d_pct) }}%</el-descriptions-item><el-descriptions-item label="SMA 5">{{ displayValue(stockStudy.technical.sma_5) }}</el-descriptions-item><el-descriptions-item label="SMA 20">{{ displayValue(stockStudy.technical.sma_20) }}</el-descriptions-item><el-descriptions-item label="RSI 14">{{ displayValue(stockStudy.technical.rsi_14) }}</el-descriptions-item><el-descriptions-item label="数据日期">{{ displayValue(stockStudy.technical.as_of_date) }}</el-descriptions-item></el-descriptions></el-card></el-col>
                </el-row>
                <el-card shadow="never" header="按需补齐状态">
                  <el-alert :title="stockStudy.on_demand_readiness?.decision_ready ? '该股票近窗口 P0 数据已补齐，可以进入单票研究。' : `仍缺 P0：${stockStudy.on_demand_readiness?.blockers.join(', ') || '-'}`" :type="stockStudy.on_demand_readiness?.decision_ready ? 'success' : 'warning'" :closable="false" show-icon />
                  <el-table :data="stockStudy.on_demand_readiness?.items ?? []" size="small" max-height="260" class="section-gap">
                    <el-table-column prop="label" label="数据" min-width="140"/>
                    <el-table-column prop="api_name" label="API" width="130"/>
                    <el-table-column prop="priority" label="优先级" width="80"/>
                    <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="featureStatusType(row.status)">{{ row.status }}</el-tag></template></el-table-column>
                    <el-table-column prop="rows" label="窗口行数" width="100"/>
                    <el-table-column prop="latest_date" label="最新日期" width="120"/>
                  </el-table>
                </el-card>
                <el-row :gutter="14">
                  <el-col :md="12" :xs="24"><el-card shadow="never" header="公司与实时数据"><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in studyMarketRecord('profile')" :key="String(key)" :label="String(key)">{{ displayValue(value) }}</el-descriptions-item></el-descriptions><el-divider content-position="left">主源与超级源实时分钟（仅交易时段）</el-divider><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_realtime')" :key="String(key)">{{ displayValue(value) }}</el-descriptions-item></el-descriptions><el-divider content-position="left">东方财富公开报价</el-divider><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in studyMarketRecord('eastmoney_quote')" :key="`east-${String(key)}`" :label="String(key)">{{ displayValue(value) }}</el-descriptions-item></el-descriptions><el-divider content-position="left">新浪财经公开报价</el-divider><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in studyMarketRecord('sina_quote')" :key="`sina-${String(key)}`" :label="String(key)">{{ displayValue(value) }}</el-descriptions-item></el-descriptions></el-card></el-col>
                  <el-col :md="12" :xs="24"><el-card shadow="never" header="估值、交易约束、资金与筹码"><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_daily_basic')" :key="`basic-${String(key)}`" :label="`估值 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_limit')" :key="`limit-${String(key)}`" :label="`涨跌停 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_adj_factor')" :key="`adj-${String(key)}`" :label="`复权 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_moneyflow')" :key="`flow-${String(key)}`" :label="`资金 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_ths_moneyflow')" :key="`ths-${String(key)}`" :label="`同花顺 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_chip')" :key="`chip-${String(key)}`" :label="`筹码 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item><el-descriptions-item v-for="(value, key) in studyMarketRecord('latest_chip_distribution')" :key="`chips-${String(key)}`" :label="`筹码分布 ${String(key)}`">{{ displayValue(value) }}</el-descriptions-item></el-descriptions></el-card></el-col>
                </el-row>
                <el-card shadow="never" header="东财主力与散户资金"><el-descriptions :column="3" border><el-descriptions-item label="交易日">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').trade_date) }}</el-descriptions-item><el-descriptions-item label="主力净流入">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').net_amount) }}</el-descriptions-item><el-descriptions-item label="主力占比">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').net_amount_rate) }}</el-descriptions-item><el-descriptions-item label="超大单净额">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_elg_amount) }}</el-descriptions-item><el-descriptions-item label="超大单占比">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_elg_amount_rate) }}</el-descriptions-item><el-descriptions-item label="大单净额">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_lg_amount) }}</el-descriptions-item><el-descriptions-item label="大单占比">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_lg_amount_rate) }}</el-descriptions-item><el-descriptions-item label="中单净额">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_md_amount) }}</el-descriptions-item><el-descriptions-item label="小单净额">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_sm_amount) }}</el-descriptions-item><el-descriptions-item label="小单占比">{{ displayValue(studyMarketRecord('latest_dc_moneyflow').buy_sm_amount_rate) }}</el-descriptions-item></el-descriptions></el-card>
                <el-card shadow="never" header="官方公告事件"><el-table :data="stockStudy.events?.announcements ?? []" size="small" max-height="260"><el-table-column prop="event_type" label="类型" width="130"/><el-table-column prop="occurred_at" label="披露时间" width="165"><template #default="{ row }">{{ dateText(row.occurred_at) }}</template></el-table-column><el-table-column prop="title" label="标题" min-width="260" show-overflow-tooltip/><el-table-column label="文件" width="78"><template #default="{ row }"><el-link v-if="row.url" :href="row.url" target="_blank" type="primary">PDF</el-link><span v-else>-</span></template></el-table-column></el-table></el-card>
                <el-card shadow="never" header="数据源执行结果"><el-table :data="stockStudy.sources" size="small"><el-table-column prop="source" label="输入" min-width="150"/><el-table-column prop="provider" label="实际来源" min-width="130"/><el-table-column prop="api_name" label="接口" width="140"/><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="sourceType(row.status)">{{ row.status }}</el-tag></template></el-table-column><el-table-column prop="received" label="响应" width="80"/><el-table-column prop="stored" label="证据" width="80"/><el-table-column prop="error" label="错误" show-overflow-tooltip/></el-table></el-card>
                <el-row :gutter="14"><el-col :md="14" :xs="24"><el-card shadow="never" header="最近日线"><el-table :data="studyBars" max-height="400" size="small"><el-table-column label="日期" width="115"><template #default="{ row }">{{ displayValue(row.trade_date) }}</template></el-table-column><el-table-column label="开盘"><template #default="{ row }">{{ displayValue(row.open) }}</template></el-table-column><el-table-column label="最高"><template #default="{ row }">{{ displayValue(row.high) }}</template></el-table-column><el-table-column label="最低"><template #default="{ row }">{{ displayValue(row.low) }}</template></el-table-column><el-table-column label="收盘"><template #default="{ row }">{{ displayValue(row.close) }}</template></el-table-column><el-table-column label="成交量"><template #default="{ row }">{{ displayValue(row.vol) }}</template></el-table-column></el-table></el-card></el-col><el-col :md="10" :xs="24"><el-card shadow="never" header="远端分析师证据"><el-descriptions :column="2" border><el-descriptions-item label="观点数">{{ displayValue(stockStudy.analyst.summary.claim_count) }}</el-descriptions-item><el-descriptions-item label="聚合方向">{{ displayValue(stockStudy.analyst.summary.direction) }}</el-descriptions-item><el-descriptions-item label="加权分">{{ displayValue(stockStudy.analyst.summary.score) }}</el-descriptions-item><el-descriptions-item label="偏多/偏空">{{ displayValue(stockStudy.analyst.summary.positive) }} / {{ displayValue(stockStudy.analyst.summary.negative) }}</el-descriptions-item></el-descriptions><el-table :data="stockStudy.analyst.claims" max-height="270" size="small" class="section-gap"><el-table-column prop="analyst_name" label="分析师"/><el-table-column label="方向" width="70"><template #default="{ row }"><el-tag :type="row.direction > 0 ? 'success' : row.direction < 0 ? 'danger' : 'info'">{{ claimDirection(row.direction) }}</el-tag></template></el-table-column><el-table-column prop="evidence" label="证据" show-overflow-tooltip/></el-table></el-card></el-col></el-row>
              </template>
              <el-empty v-else-if="!studyLoading" description="输入股票代码后开始研究" :image-size="80" />
            </el-tab-pane>
            <el-tab-pane label="分析师证据" name="evidence">
              <el-row :gutter="14"><el-col :md="12" :xs="24"><el-card shadow="never" header="远端报告"><el-table :data="reports" max-height="560"><el-table-column prop="report_date" label="日期" width="110"/><el-table-column prop="analyst_name" label="分析师" width="120"/><el-table-column prop="title" label="标题"/><el-table-column prop="summary" label="摘要" show-overflow-tooltip/></el-table></el-card></el-col><el-col :md="12" :xs="24"><el-card shadow="never" header="结构化观点"><el-table :data="claims" max-height="560"><el-table-column prop="analyst_name" label="分析师" width="105"/><el-table-column prop="subject_label" label="标的"/><el-table-column label="方向" width="76"><template #default="{ row }"><el-tag :type="row.direction > 0 ? 'success' : row.direction < 0 ? 'danger' : 'info'">{{ claimDirection(row.direction) }}</el-tag></template></el-table-column><el-table-column label="方向来源" width="140"><template #default="{ row }">{{ row.direction_source === 'explicit_action_positive' ? '明确调入/买入' : row.direction_source === 'explicit_action_negative' ? '明确调出/卖出' : row.direction_source === 'lexical_context' ? '上下文词' : '中性提及' }}</template></el-table-column><el-table-column prop="horizon_days" label="周期" width="65"/><el-table-column prop="evidence" label="证据" show-overflow-tooltip/></el-table></el-card></el-col></el-row>
            </el-tab-pane>
            <el-tab-pane label="观点复核" name="claim-review">
              <el-card shadow="never" header="待映射分析师标的"><el-alert title="无法精确识别为股票代码的远端文本必须人工映射后才能参与股票级评分。" type="warning" :closable="false" show-icon/><el-table :data="claimReviews" max-height="560" class="section-gap"><el-table-column prop="analyst_name" label="分析师" width="120"/><el-table-column prop="suggested_label" label="原始标的"/><el-table-column label="方向" width="80"><template #default="{ row }"><el-tag :type="recommendationType(row.direction)">{{ claimDirection(row.direction) }}</el-tag></template></el-table-column><el-table-column prop="horizon_days" label="周期" width="80"/><el-table-column label="映射代码" width="160"><template #default="{ row }"><el-input v-model="reviewSymbol[row.review_id]" :placeholder="row.suggested_symbol || '000636.SZ'"/></template></el-table-column><el-table-column prop="evidence" label="证据" show-overflow-tooltip/><el-table-column label="操作" width="160"><template #default="{ row }"><el-button link type="success" @click="decideReview(row,'approved')">批准</el-button><el-button link type="danger" @click="decideReview(row,'rejected')">拒绝</el-button></template></el-table-column></el-table></el-card>
            </el-tab-pane>
            <el-tab-pane label="数据源 Doctor" name="providers">
              <el-card shadow="never" class="realtime-health-panel">
                <template #header><div class="card-header"><div><span>盘中实时链路与日终摘要</span><small class="realtime-refresh-time">{{ realtimeServices.session_active ? (realtimeServices.special_window_active ? '连续竞价 · 特别关注窗口' : '连续竞价 · 常规窗口') : '休市/非连续竞价 · 服务待命；日终摘要独立按 19:15 窗口运行' }} · 更新于 {{ dateText(realtimeServices.observed_at) }}</small></div><el-space><el-tag :type="adapterHealth.status === 'ok' ? 'success' : 'danger'">适配器 {{ adapterHealth.status === 'ok' ? '正常' : '异常' }}</el-tag><el-tag :type="adapterHealth.quant_alert_configured ? 'success' : 'danger'">飞书 {{ adapterHealth.quant_alert_configured ? '已配置' : '未配置' }}</el-tag><el-button :icon="Refresh" :loading="realtimeLoading" @click="loadRealtimeServices">刷新</el-button></el-space></div></template>
                <el-alert v-if="realtimeError" :title="`实时状态读取失败：${realtimeError}`" type="error" :closable="false" show-icon/>
                <el-alert v-else-if="realtimeServices.summary?.decision_path_degraded" title="当前应运行的决策链路存在延迟或降级，请先检查对应数据源，系统不会把过期数据标记为健康。" type="error" :closable="false" show-icon/>
                <el-alert v-else :title="realtimeServices.session_active ? `盘中链路正在按计划运行，观察池 ${realtimeServices.summary?.enabled_watch_count ?? 0} 只。` : `当前为待命状态：${realtimeServices.session_reason ?? '非交易时段'}。待命不等于故障。`" :type="realtimeServices.session_active ? 'success' : 'info'" :closable="false" show-icon/>
                <el-row :gutter="12" class="realtime-service-grid">
                  <el-col v-for="service in realtimeServices.items ?? []" :key="service.key" :xs="24" :sm="12" :lg="8">
                    <el-card shadow="hover" class="realtime-service-card">
                      <div class="realtime-service-head"><strong>{{ service.label }}</strong><el-tag :type="realtimeStateType(service.state)" effect="dark" size="small">{{ realtimeStateText(service.state) }}</el-tag></div>
                      <p class="realtime-service-role">{{ service.role }}</p>
                      <el-descriptions :column="1" size="small" border>
                        <el-descriptions-item label="运行窗口">{{ service.expected_active ? '当前应运行' : '当前不轮询' }}</el-descriptions-item>
                        <el-descriptions-item label="频率">{{ service.cadence }}</el-descriptions-item>
                        <el-descriptions-item label="最新数据">{{ dateText(service.last_observed_at) }}</el-descriptions-item>
                        <el-descriptions-item label="数据年龄">{{ ageText(service.age_seconds) }}<span v-if="service.max_age_seconds"> / 门限 {{ ageText(service.max_age_seconds) }}</span></el-descriptions-item>
                        <el-descriptions-item label="延迟/行数">{{ service.last_latency_ms ?? '-' }} ms / {{ service.last_row_count ?? '-' }}</el-descriptions-item>
                        <el-descriptions-item v-if="realtimeDeliveryDetail(service)" label="投递状态">{{ realtimeDeliveryDetail(service) }}</el-descriptions-item>
                      </el-descriptions>
                      <el-tooltip v-if="service.last_error" :content="service.last_error" placement="top"><el-text class="realtime-service-error" type="danger" truncated>最近错误：{{ service.last_error }}</el-text></el-tooltip>
                    </el-card>
                  </el-col>
                </el-row>
              </el-card>
              <el-card shadow="never" header="外部数据源"><el-table :data="catalog.providers ?? []"><el-table-column prop="label" label="来源" min-width="170"/><el-table-column prop="provider_key" label="Provider key" min-width="160"/><el-table-column prop="protocol" label="协议" width="120"/><el-table-column label="限频" width="128"><template #default="{ row }">{{ row.rate_limit_per_minute ? `${row.rate_limit_per_minute}/分` : '-' }}{{ row.min_interval_seconds ? ` · ≥${row.min_interval_seconds}秒` : '' }}</template></el-table-column><el-table-column label="实时覆盖" min-width="170"><template #default="{ row }"><el-tag size="small" :type="row.realtime_coverage === 'verified_partial' ? 'success' : row.realtime_coverage === 'unavailable' ? 'danger' : 'info'">{{ row.realtime_coverage === 'verified_partial' ? `已实测 ${row.realtime_apis?.length ?? 0} 项` : row.realtime_coverage === 'unavailable' ? '明确不可用' : row.realtime_coverage ?? '-' }}</el-tag><small class="catalog-check-time">{{ row.realtime_note ?? '' }}</small></template></el-table-column><el-table-column label="Super 路由首选" min-width="190"><template #default="{ row }"><span v-if="row.name === 'super_sdk' || row.name === 'super_get'">{{ row.super_alias_first_apis?.length ?? 0 }} 项</span><span v-else>-</span><small v-if="row.super_alias_first_apis?.length" class="catalog-check-time">{{ row.super_alias_first_apis.join(', ') }}</small></template></el-table-column><el-table-column label="完整性边界" min-width="210"><template #default="{ row }"><span v-if="row.name === 'super_get'">完整 {{ row.complete_query_apis?.length ?? 0 }} 项；受限 {{ row.bounded_only_apis?.join(', ') || '无' }}；需对账 {{ row.reconciliation_required_apis?.join(', ') || '无' }}</span><span v-else>{{ row.name === 'super_sdk' ? '广域成员/事件完整源' : row.name === 'primary' ? '全A名录基准；无实时能力' : '-' }}</span></template></el-table-column><el-table-column label="配置" width="100"><template #default="{ row }"><el-tag :type="row.configured ? 'success' : 'info'">{{ row.configured ? '已配置' : '未配置' }}</el-tag></template></el-table-column></el-table></el-card>
              <el-card shadow="never" header="能力健康"><el-table :data="providerHealth" max-height="500"><el-table-column prop="label" label="来源"/><el-table-column prop="capability" label="能力"/><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="healthState(row)">{{ row.circuit_open_until ? '已熔断' : row.last_error ? '异常' : row.last_success_at ? '正常' : '未运行' }}</el-tag></template></el-table-column><el-table-column label="最近成功" width="175"><template #default="{ row }">{{ dateText(row.last_success_at) }}</template></el-table-column><el-table-column prop="last_latency_ms" label="延迟 ms" width="100"/><el-table-column prop="last_row_count" label="行数" width="80"/><el-table-column prop="last_error" label="最近错误" show-overflow-tooltip/></el-table></el-card>
            </el-tab-pane>
            <el-tab-pane label="接口与原始数据" name="catalog">
              <el-row :gutter="14" class="metric-row">
                <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>接口库存</span><strong>{{ catalogCount('total') }}</strong></el-card></el-col>
                <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>官方积分扩展</span><strong>{{ catalogCount('points_at_or_below_15000') }}</strong></el-card></el-col>
                <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>实时接口</span><strong>{{ catalogCount('market_hours_only') }}</strong></el-card></el-col>
                <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>主 / SDK / GET 验证</span><strong>{{ catalogCount('primary_verified') }} / {{ catalogCount('super_sdk_verified') }} / {{ catalogCount('super_get_verified') }}</strong></el-card></el-col>
              </el-row>
              <el-card shadow="never">
                <template #header><div class="card-header"><span>物理通道权限与完整性矩阵</span><el-space><el-button :icon="Refresh" :loading="catalogRefreshing" @click="refreshCatalog">刷新状态</el-button><el-button :disabled="!selectedCatalog.length" :loading="actionLoading === '核验所选接口'" @click="auditSelectedCatalog">核验所选（{{ selectedCatalog.length }}）</el-button><el-button type="primary" @click="openFetch()">读取数据</el-button></el-space></div></template>
                <el-alert title="目录登记不等于可用，返回成功也不等于完整。主源、Super SDK、Super GET 分开记账；GET 的 ths_member 仅限小结果，完整板块快照固定走 SDK。" type="info" :closable="false" show-icon class="section-gap"/>
                <div class="table-toolbar"><el-input v-model="catalogQuery" placeholder="搜索 API、类别或模型用途" clearable/><el-select v-model="catalogGroup"><el-option v-for="group in catalogGroups" :key="group" :label="group === 'all' ? '全部类别' : group" :value="group"/></el-select><el-tag type="info">{{ visibleCatalog.length }} / {{ catalog.count ?? 0 }}</el-tag><el-tag type="warning">历史分钟 {{ catalogCount('offline_files_only') }} 项仅文件导入</el-tag></div>
                <el-table v-if="!mobileLayout" :data="visibleCatalog" row-key="api_name" max-height="560" @selection-change="selectCatalog">
                  <el-table-column type="selection" width="40"/>
                  <el-table-column prop="api_name" label="API" width="140" fixed/>
                  <el-table-column label="分类 / 模型用途" min-width="190"><template #default="{ row }"><div class="catalog-purpose"><span>{{ row.group }}</span><small>{{ row.model_role }}</small></div></template></el-table-column>
                  <el-table-column label="权限" width="92"><template #default="{ row }"><el-tag size="small" type="info">{{ permissionText(row) }}</el-tag></template></el-table-column>
                  <el-table-column label="策略" width="96"><template #default="{ row }"><el-tag size="small" :type="row.request_policy === 'market_hours_only' ? 'warning' : row.request_policy === 'offline_files_only' ? 'info' : 'success'">{{ policyText(row.request_policy) }}</el-tag></template></el-table-column>
                  <el-table-column label="主源" width="112"><template #default="{ row }"><el-tag size="small" :type="availabilityType(row.primary_availability)">{{ availabilityText(row.primary_availability) }}</el-tag><small class="catalog-check-time">{{ observationText(row, 'tushare_primary') }}</small></template></el-table-column>
                  <el-table-column label="Super SDK" width="112"><template #default="{ row }"><el-tag size="small" :type="availabilityType(row.super_sdk_availability)">{{ availabilityText(row.super_sdk_availability) }}</el-tag><small class="catalog-check-time">{{ observationText(row, 'tushare_super_sdk') }}</small></template></el-table-column>
                  <el-table-column label="Super GET" width="112"><template #default="{ row }"><el-tag size="small" :type="availabilityType(row.super_get_availability)">{{ availabilityText(row.super_get_availability) }}</el-tag><small class="catalog-check-time">{{ observationText(row, 'tushare_super_get') }}</small></template></el-table-column>
                  <el-table-column label="入模" width="82"><template #default="{ row }"><el-tag :type="row.normalized ? 'success' : 'info'">{{ row.normalized ? '标准化' : '仅证据' }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="64" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openFetch(row)">读取</el-button></template></el-table-column>
                </el-table>
                <el-table v-else :data="visibleCatalog" row-key="api_name" max-height="520" size="small" @selection-change="selectCatalog">
                  <el-table-column type="selection" width="36"/>
                  <el-table-column label="API / 权限" min-width="96"><template #default="{ row }"><div class="catalog-mobile-api"><strong>{{ row.api_name }}</strong><small>{{ permissionText(row) }}</small></div></template></el-table-column>
                  <el-table-column label="物理通道" width="104"><template #default="{ row }"><div class="catalog-mobile-status"><el-tag size="small" :type="availabilityType(row.primary_availability)">主 {{ availabilityText(row.primary_availability) }}</el-tag><el-tag size="small" :type="availabilityType(row.super_sdk_availability)">SDK {{ availabilityText(row.super_sdk_availability) }}</el-tag><el-tag size="small" :type="availabilityType(row.super_get_availability)">GET {{ availabilityText(row.super_get_availability) }}</el-tag></div></template></el-table-column>
                  <el-table-column label="操作" width="52"><template #default="{ row }"><el-button link type="primary" @click="openFetch(row)">读取</el-button></template></el-table-column>
                </el-table>
              </el-card>
              <el-card v-if="auditResults.length" shadow="never" header="最近物理通道核验结果"><el-table :data="auditResults" max-height="360" size="small"><el-table-column prop="api_name" label="API" width="170"/><el-table-column prop="provider" label="来源" width="110"/><el-table-column label="状态" width="112"><template #default="{ row }"><el-tag :type="availabilityType(row.availability)">{{ availabilityText(row.availability) }}</el-tag></template></el-table-column><el-table-column prop="received" label="响应" width="75"/><el-table-column prop="stored" label="证据" width="75"/><el-table-column prop="reason" label="说明" show-overflow-tooltip/></el-table></el-card>
            </el-tab-pane>
            <el-tab-pane label="质量与分钟数据" name="quality">
              <el-row :gutter="14"><el-col :md="14" :xs="24"><el-card shadow="never" header="未解决质量问题"><el-table :data="qualityIssues" max-height="490"><el-table-column prop="severity" label="级别" width="85"><template #default="{ row }"><el-tag :type="row.severity === 'blocking' || row.severity === 'error' ? 'danger' : 'warning'">{{ row.severity }}</el-tag></template></el-table-column><el-table-column prop="capability" label="能力"/><el-table-column prop="symbol" label="标的"/><el-table-column prop="code" label="代码"/><el-table-column prop="message" label="说明" show-overflow-tooltip/></el-table></el-card></el-col><el-col :md="10" :xs="24"><el-card shadow="never" header="离线分钟导入"><el-alert :title="minuteDirectory || '离线目录未返回'" type="info" :closable="false" show-icon/><el-table :data="minuteImports" size="small" max-height="410" class="section-gap"><el-table-column prop="file_name" label="文件" show-overflow-tooltip/><el-table-column prop="status" label="状态" width="80"/><el-table-column prop="row_count" label="行数" width="90"/><el-table-column prop="rejected_rows" label="拒绝" width="70"/></el-table></el-card></el-col></el-row>
            </el-tab-pane>
          </el-tabs>
        </template>
        <template v-else-if="activeSection === 'monitor'"><el-card shadow="never"><template #header><div class="card-header"><span>导入事件</span><el-select v-model="eventFilter" size="small" class="event-filter"><el-option label="全部状态" value="all"/><el-option label="已完成" value="已完成"/><el-option label="失败" value="失败"/><el-option label="处理中" value="已接收，处理中"/></el-select></div></template><el-empty v-if="!visibleEvents.length" description="暂无事件"/><el-timeline v-else><el-timeline-item v-for="event in visibleEvents" :key="event.event_id" :timestamp="dateText(event.received_at)" :type="event.n8n_status === '失败' ? 'danger' : 'primary'"><el-card shadow="never"><div class="event-title"><strong>{{ event.message_type || 'message' }}</strong><el-tag size="small">{{ event.n8n_status || '未知' }}</el-tag></div><p v-if="event.text">{{ event.text }}</p><el-text type="info">{{ event.source_label || '无来源备注' }}{{ event.n8n_error ? ` · ${event.n8n_error}` : '' }}</el-text></el-card></el-timeline-item></el-timeline></el-card></template>
        <template v-else><el-card shadow="never" header="手动投递"><el-form label-position="top"><el-row :gutter="14"><el-col :md="12" :xs="24"><el-form-item label="来源"><el-select v-model="relayTag" class="full-width"><el-option v-for="route in routes" :key="route.tag" :label="`#${route.tag} · ${route.label}`" :value="route.tag"/></el-select></el-form-item></el-col><el-col :md="12" :xs="24"><el-form-item label="来源备注"><el-input v-model="relaySource"/></el-form-item></el-col><el-col :md="12" :xs="24"><el-form-item label="日期"><el-date-picker v-model="relayDate" value-format="YYYY-MM-DD" type="date" class="full-width"/></el-form-item></el-col><el-col :md="12" :xs="24"><el-form-item label="时间"><el-time-picker v-model="relayTime" value-format="HH:mm" format="HH:mm" class="full-width"/></el-form-item></el-col></el-row><el-form-item label="正文"><el-input v-model="relayText" type="textarea" :rows="8"/></el-form-item><el-form-item label="媒体"><el-upload drag :auto-upload="false" :show-file-list="false" :on-change="(file: { raw?: File }) => file.raw && addFiles([file.raw])"><el-icon class="upload-icon"><UploadFilled /></el-icon><div>选择文件或拖入此处</div></el-upload><el-space wrap class="section-gap"><el-tag v-for="file in relayFiles" :key="file.name + file.size" closable @close="relayFiles = relayFiles.filter((item) => item !== file)">{{ file.name }}</el-tag></el-space></el-form-item><el-progress v-if="relayXhr" :percentage="relayProgress"/><el-alert v-if="relayState" :title="relayState" type="info" :closable="false" class="section-gap"/><el-button type="primary" :loading="!!relayXhr" @click="submitRelay">开始投递</el-button><el-button v-if="relayXhr" @click="relayXhr?.abort(); relayXhr = null">取消</el-button></el-form></el-card></template>
      </el-main>
    </el-container>
  </el-container>
  <el-dialog v-model="fetchDialogOpen" title="受控数据读取" width="680px" destroy-on-close><el-form label-position="top"><el-row :gutter="14"><el-col :span="12"><el-form-item label="API"><el-input v-model="fetchForm.api_name"/></el-form-item></el-col><el-col :span="12"><el-form-item label="来源"><el-select v-model="fetchForm.provider" class="full-width"><el-option label="自动回退" value="auto"/><el-option label="主 Tushare 源" value="primary"/><el-option label="Super 聚合兼容路由" value="super"/><el-option label="Super SDK 完整路径" value="super_sdk"/><el-option label="Super GET 已验证路径" value="super_get"/><el-option label="REST 备用源" value="backup"/></el-select></el-form-item></el-col></el-row><el-alert title="完整 ths_member 请选自动、Super 聚合或 Super SDK；Super GET 对大板块会被上游截断。" type="warning" :closable="false" class="section-gap"/><el-form-item label="参数 JSON"><el-input v-model="fetchForm.paramsText" type="textarea" :rows="8" class="mono"/></el-form-item><el-row :gutter="14"><el-col :span="16"><el-form-item label="字段"><el-input v-model="fetchForm.fields"/></el-form-item></el-col><el-col :span="8"><el-form-item label="最大行数"><el-input-number v-model="fetchForm.max_rows" :min="1" :max="10000" class="full-width"/></el-form-item></el-col></el-row></el-form><template #footer><el-button @click="fetchDialogOpen = false">取消</el-button><el-button type="primary" :loading="actionLoading === 'fetch'" @click="executeFetch">读取并保存证据</el-button></template></el-dialog>
  <el-dialog v-model="fetchResultOpen" title="读取结果" width="620px"><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in fetchResult" :key="String(key)" :label="String(key)"><span class="result-value">{{ typeof value === 'object' ? JSON.stringify(value) : value }}</span></el-descriptions-item></el-descriptions></el-dialog>
</template>

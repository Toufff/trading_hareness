/** Wire types of the read-only discipline routes (/api/research/discipline/...). Decimals arrive as strings. */

export type Num = number | string | null | undefined;

export type DisciplineLine = {
  kind: string;
  label: string;
  metric?: string | null;
  op?: string | null;
  price?: Num;
  pct?: Num;
  confirm?: { bars?: number; basis?: 'daily' | 'minute' };
  extra?: string[];
  execute_by?: 'price' | 'time';
  execute_at?: string | null;
  trading_days?: number | null;
  action: { type: string; value?: Num };
  derivation: {
    rule_id: string;
    inputs?: Record<string, unknown>;
    formula?: string;
    action_inputs?: Record<string, unknown>;
    action_formula?: string;
  };
  priority: number;
};

export type DisciplineSizing = {
  equity: Num; risk_per_trade_pct: Num; reference_price: Num; hard_stop: Num; stop_distance: Num;
  risk_amount: Num; max_shares: number; target_exposure_pct: Num; current_shares: number;
  current_exposure_pct: Num; recommended_shares: number; current_risk_pct?: Num;
};

export type DisciplinePosition = {
  snapshot_id: string; observed_at: string; quantity: number; sellable_quantity: number;
  average_cost?: Num; market_price?: Num; market_value?: Num;
};

export type QualityCheck = { check_id: string; passed: boolean; detail?: string };

export type OmittedLine = { kind: string; reason: string; inputs?: Record<string, unknown> };

export type EntryBasis = {
  lane_reference: number; lane_reference_source?: string; last_close: number; last_close_date: string;
  last_close_basis?: 'settled' | 'forming'; entry_price: number; entry_source?: string; formula?: string;
};

export type RecommendationConditions = {
  decision_id?: string; as_of_date?: string; trigger?: string; invalidation?: string; why_now?: string;
  note?: string; evaluable?: boolean;
};

export type PlanMetrics = Record<string, unknown> & {
  close?: number; atr14?: number; ma5?: number; ma10?: number; ma20?: number; low20?: number;
  omitted_lines?: OmittedLine[];
  calendar?: { sessions?: string[]; closure_gaps?: Array<Record<string, unknown>> };
  entry?: EntryBasis;
  recommendation_conditions?: RecommendationConditions;
  t1_locked_shares?: number;
  t1_snapshot_at?: string | null;
  stage_decision?: { stage?: string; reason?: string; lane?: string | null };
};

export type DisciplinePlan = {
  plan_id: string;
  run_id?: string;
  plan_key: string;
  account_key: string;
  symbol: string;
  name: string;
  plan_kind: 'holding' | 'new_buy';
  stage: string;
  template_key?: string;
  template_version?: string;
  as_of_at: string;
  trading_date: string;
  valid_until: string;
  position?: DisciplinePosition | null;
  metrics: PlanMetrics;
  sizing?: DisciplineSizing | null;
  lines: DisciplineLine[];
  evidence_refs?: string[];
  quality?: QualityCheck[];
  status: 'active' | 'rejected_by_quality' | 'superseded' | 'expired';
  supersedes_plan_id?: string | null;
  lowered_reason?: string | null;
  inputs_hash?: string;
  generator_version?: string;
  content_hash?: string;
  created_at?: string;
};

export type PlansResponse = { account_key: string; statuses: string[]; count: number; items: DisciplinePlan[] };

export type ChartBar = {
  date: string; open: number; high: number; low: number; close: number; volume: number | null; amount: number | null;
  change_pct: number | null; ma5: number | null; ma10: number | null; ma20: number | null; atr14: number | null;
  atr_upper?: number | null; atr_lower?: number | null;
};

export type Closure = { last_trading_date: string; resume_date: string; closed_days: number; dates: string[]; label: string };

export type StructurePoint = { key: string; label: string; price: number; date: string | null; role: string; source: string };

export type HardStopTerms = {
  price: number | null; formula?: string; rule_id?: string; binding_term?: string; structure_source?: string;
  reference_price: number; atr14: number; terms: Array<{ term: string; label: string; value: number | null }>;
};

export type DailyChart = {
  basis: 'daily';
  plan_id: string; symbol: string; name: string; plan_kind: string;
  trading_date: string; valid_until: string; as_of: string;
  price_basis: { kind: string; table?: string; note: string; corporate_actions: Array<{ date: string; pre_close: number; previous_close: number }>;
    lines_comparable: boolean; warning: string | null };
  plan_bar_matches_metrics: boolean | null;
  bars: ChartBar[];
  sessions: string[];
  future_sessions: string[];
  closures: Closure[];
  suspensions?: Array<{ from: string; to: string; missing_sessions: number; label: string }>;
  structure_points: StructurePoint[];
  hard_stop_terms: HardStopTerms | null;
  latest: { date: string; close: number; atr14: number | null } | null;
};

export type MinuteRow = { time: string; open: number | null; high: number | null; low: number | null; close: number; volume: number | null; amount: number | null; vwap: number | null; close_only?: boolean };

export type MinuteChart = {
  basis: 'minute'; plan_id: string; symbol: string; date: string; source: string | null;
  rows: MinuteRow[]; count: number; reason: string | null; bar_type?: 'ohlc' | 'close_only';
};

export type LineStateEntry = {
  trading_date: string | null; as_of_at: string | null; basis: string; state: string;
  triggered_at: string | null; trigger_price: number | null;
};

export type EvaluationTransition = LineStateEntry & {
  line_index: number; kind: string; label: string; from: string; to: string; date: string | null;
};

export type EvaluationsResponse = {
  plan_id: string; plan_status?: string; count: number;
  evaluations: Array<{ as_of_at: string; trading_date: string; basis: string; plan_state: string; line_states: Array<Record<string, unknown>> }>;
  lines: Array<{ index: number; kind: string; label: string; basis: string; states: LineStateEntry[] }>;
  transitions: EvaluationTransition[];
  plan_state_changes: Array<{ trading_date: string | null; as_of_at: string | null; basis: string; from: string | null; to: string }>;
};

export type LadderStep = {
  plan_id: string; plan_key: string; status: string; plan_kind: string; trading_date: string; until: string;
  hard_stop: number | null; previous_hard_stop: number | null; lowered: boolean; lowered_reason: string | null;
  supersedes_plan_id: string | null;
};

export type HistoryResponse = { account_key: string; symbol: string; count: number; items: DisciplinePlan[]; ladder: LadderStep[] };

export type TradeFill = {
  record_id: string; trade_date: string; trade_time: string | null; symbol: string; name?: string | null;
  side: 'buy' | 'sell' | string; quantity: Num; price: Num;
  verdicts: Array<{ verdict: string; line_kind: string | null; notes: string | null; plan_id: string }>;
};

export type ReconciliationsResponse = {
  account_key: string; symbol: string | null; from: string; to: string;
  items: Array<Record<string, unknown> & { verdict: string; line_kind: string | null; trade_record_id: string | null; notes: string | null }>;
  trades: TradeFill[];
  verdict_counts: Record<string, number>;
};

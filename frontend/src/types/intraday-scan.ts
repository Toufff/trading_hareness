import type { EventResearch } from '../components/EventResearchPanel.vue';
export type ScanItem = {
  symbol: string; name: string; lane: string; state: string; reason: string; original_reason?: string;
  price?: number; change_pct?: number; amount?: number; main_net?: number; reference?: number; support?: number;
  matched_today?: boolean; evidence_gaps?: string[]; entry_scenario?: string; amount_basis?: string;
  current_reason?: string;
  chart?: {time: string; close: number; vwap?: number; amount?: number}[];
  ohlc?: {date: string; open: number; close: number; low: number; high: number}[];
  plan?: {state: string; created_at: string};
};
export type ScanLane = {key: string; label: string; status: string; total_matches: number; items: ScanItem[]; data_gaps?: Record<string,number>};
export type ScanResponse = {
  status: string; run_id?: string; detail?: ScanItem;
  runs: {run_id: string; cutoff: string; state: string; stage: string; error?: string; model_version: string}[];
  result: null | {version: string; phase: string; cutoff: string; observed_at: string; ohlc_captured_at?: string;
    event_research?: EventResearch;
    input_hash: string; lanes: ScanLane[]; market: {symbols: number; up: number; down: number; median: number};
    history_health?: {requested: number; ready: number}; minute_health?: {requested: number; received: number}};
  reconciliations?: {source_run_id: string; source_cutoff: string; compared: number}[];
  changes?: {source_cutoff: string; observation_change_pct: number|null; source_state: string; close_state: string; plan_state?: string}[];
};

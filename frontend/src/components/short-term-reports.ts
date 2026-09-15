import type { ReviewProjection } from './ReviewSelectionPanel.vue';
import type { Followup } from './ObservationFollowup.vue';
import type { Effectiveness } from './StrategyEffectiveness.vue';
import type { PriceVolumeEvidence } from './price-volume-evidence';
import type { EventResearch } from './EventResearchPanel.vue';

export type StrategyPick = {
  symbol: string; name: string; reason: string; confirmation: string; invalidation: string;
  caution: string; expiry: string; sector_label: string;
  price_volume?: PriceVolumeEvidence;
  liquidity?: { score: number };
  attention?: { participation_percentile: number; sample_count: number; amount_multiple: number | null };
  metrics: { close: number; change_pct: number; amount: number; turnover: number };
  factor_overlay?: { explanation: string; rank_scope: string; base_rank: number;
    adjusted_rank: number; bonus: number; factors: { key: string; status: string; explanation: string; bonus: number }[] };
};
export type StrategyLane = {
  key: string; label: string; purpose: string; total_matches: number;
  selected: StrategyPick[]; caution_list?: StrategyPick[]; empty_reason: string;
  observation_list?: StrategyPick[]; observation_policy?: string;
  factor_policy?: { enabled: boolean; rule: string; factors: { key: string; weight: number;
    context: { status: string; valid_count: number; universe_count: number; cap_basis: string } }[] };
};
export type StrategyReport = {
  key: string; title: string; filename: string; markdown: string; as_of_date: string;
  content_sha256: string;
  result_summary?: StrategyResult;
  review?: ReviewProjection & { review_coverage: {
    planned: number; completed: number; missing_symbols: string[]; selected_reviewed: number; selected_total: number;
  } };
};
export type StrategyResult = {
  key: string; label: string; conclusion: string;
  rows: { symbol: string; name: string; state: string; conclusion: string; reason: string;
    confirmation: string; invalidation: string; caution: string; expiry: string }[];
};
export type StrategyScan = ReviewProjection & {
  as_of_date: string; version: string; status: string; notice: string;
  coverage: { complete_history: number; universe: number; verified_event_symbols?: number };
  market?: { up_fraction: number; median_return10: number };
  lanes: StrategyLane[];
  followup?: Followup;
  effectiveness?: Effectiveness;
  event_research?: EventResearch;
  report_bundle?: {
    version: string; source_sha256: string; reports: StrategyReport[];
    overlaps: { symbol: string; name: string; interpretation: string;
      memberships: { key: string; label: string; state: string; reason: string }[] }[];
  };
};

/** Download the persisted report, not a separately reconstructed browser copy. */
export function downloadStrategyReport(report: StrategyReport): void {
  const url = URL.createObjectURL(new Blob([report.markdown], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = report.filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

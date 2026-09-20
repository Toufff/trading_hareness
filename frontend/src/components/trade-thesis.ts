import { getJson, postJson } from '../api/http';
import type { WorkbenchAnnotation } from './stock-workbench-control';

export type RankScope = {
  rank?: number | null;
  universe_size?: number | null;
  strategy?: string | null;
  scope?: string | null;
  comparable_to_previous?: boolean | null;
  non_comparable_reason?: string | null;
  lane?: string | null;
  population?: number | null;
  score?: number | null;
  origin_id?: string | null;
};

export type ThesisEvidenceDelta = {
  label?: string;
  metric?: string;
  old_value?: unknown;
  new_value?: unknown;
  unit?: string | null;
  benchmark?: string | null;
  evidence_id?: string | null;
  impact_scope?: string | null;
  impact?: string | null;
  directly_comparable?: boolean | null;
  old_version?: string | null;
  version?: string | null;
};

export type ThesisObservation = {
  evidence_id?: string; metric?: string; value?: unknown; unit?: string | null;
  basis?: string | null; benchmark?: string | null; available_at?: string | null;
};

export type ThesisLine = {
  id?: string;
  label: string;
  price: number;
  kind: 'original_structure' | 'current_plan';
  valid_from?: string | null;
  valid_until?: string | null;
  detail?: string | null;
  source_ref?: string | null;
};

export type TradeThesisSummary = {
  thesis_id: string;
  revision: number;
  symbol: string;
  family?: string | null;
  claim?: string | null;
  original_thesis?: string | null;
  current_phase?: string | null;
  thesis_state?: string | null;
  evidence_status?: string | null;
  entry_state?: string | null;
  new_buy?: string | null;
  holding_action?: string | null;
  holding_plan_status?: string | null;
  scenario_projection?: {
    combined_entry_state?: string;
    conflicts?: Array<string | { code?: string; message?: string }>;
    execution?: { state?: string; reason?: string; earliest_session?: string | null };
  };
  next_checks?: Array<string | Record<string, unknown>>;
  changes_since_previous?: ThesisEvidenceDelta[];
  contrary_evidence?: Array<string | Record<string, unknown>>;
  observations?: ThesisObservation[];
  original_rank?: RankScope | null;
  current_rank?: RankScope | null;
  original_rankings?: RankScope[];
  current_rankings?: RankScope[];
  cutoff_at?: string | null;
  source_run_id?: string | null;
  input_hash?: string | null;
  content_hash?: string | null;
  evaluation_id?: string | null;
  previous_evaluation_id?: string | null;
  available_at?: string | null;
  origin_mode?: string | null;
  chart_lines?: ThesisLine[];
  thesis?: Record<string, unknown>;
  evaluation?: Record<string, unknown> | null;
};

export type TradeThesisList = {
  items?: TradeThesisSummary[];
  theses?: TradeThesisSummary[];
  as_of?: string | null;
  cutoff_at?: string | null;
};

export type TradeThesisTimeline = {
  thesis_id: string;
  items?: Array<Record<string, unknown>>;
  events?: Array<Record<string, unknown>>;
  revisions?: Array<Record<string, unknown>>;
  evaluations?: Array<Record<string, unknown>>;
};

export type EvaluateThesisRequest = { source_run_id: string; thesis_id?: string; cutoff_at?: string };
export type ChangeThesisRequest = { expected_revision: number; [key: string]: unknown };
export type ReviewThesisRequest = { revision: number; proposal_hash: string; verdict: 'approve' | 'reject' | 'needs_evidence'; reviewer: string; reason: string; evidence_hash?: string };

const base = '/api/research/theses';

export function listTradeTheses(symbol: string, asOf?: string, signal?: AbortSignal) {
  const params = new URLSearchParams({ symbol: symbol.trim().toUpperCase() });
  if (asOf) params.set('as_of', asOf);
  return getJson<TradeThesisList | TradeThesisSummary[]>(`${base}?${params}`, { signal });
}

export function getTradeThesisTimeline(thesisId: string, asOf?: string, signal?: AbortSignal) {
  const params = new URLSearchParams();
  if (asOf) params.set('as_of', asOf);
  const suffix = params.size ? `?${params}` : '';
  return getJson<TradeThesisTimeline>(`${base}/${encodeURIComponent(thesisId)}/timeline${suffix}`, { signal });
}

export function evaluateTradeThesis(payload: EvaluateThesisRequest) {
  return postJson<Record<string, unknown>>(`${base}/evaluate`, payload);
}

export function proposeTradeThesisChange(thesisId: string, payload: ChangeThesisRequest) {
  return postJson<Record<string, unknown>>(`${base}/${encodeURIComponent(thesisId)}/changes`, payload);
}

export function reviewTradeThesis(thesisId: string, payload: ReviewThesisRequest) {
  return postJson<Record<string, unknown>>(`${base}/${encodeURIComponent(thesisId)}/reviews`, payload);
}

export function thesisItems(payload: TradeThesisList | TradeThesisSummary[]): TradeThesisSummary[] {
  const rows = Array.isArray(payload) ? payload : payload.items ?? payload.theses ?? [];
  return rows.map((row) => {
    const thesis = row.thesis ?? {};
    const evaluation = row.evaluation ?? {};
    const states = (evaluation.states ?? {}) as Record<string, unknown>;
    return {
      ...row,
      claim: row.claim ?? asString(thesis.claim),
      original_thesis: row.original_thesis ?? asString(thesis.claim),
      current_phase: row.current_phase ?? asString(states.thesis_state),
      thesis_state: row.thesis_state ?? asString(states.thesis_state),
      evidence_status: row.evidence_status ?? asString(states.evidence_status),
      entry_state: row.entry_state ?? asString(states.entry_state),
      changes_since_previous: row.changes_since_previous ?? evaluation.changes_since_previous as ThesisEvidenceDelta[] | undefined,
      contrary_evidence: row.contrary_evidence ?? evaluation.contrary_evidence as Array<string | Record<string, unknown>> | undefined,
      observations: row.observations ?? evaluation.observations as ThesisObservation[] | undefined,
      next_checks: row.next_checks ?? evaluation.next_checks as Array<string | Record<string, unknown>> | undefined,
      cutoff_at: row.cutoff_at ?? asString(evaluation.cutoff_at),
      source_run_id: row.source_run_id ?? asString(evaluation.source_run_id) ?? asString(thesis.source_run_id),
      content_hash: row.content_hash ?? asString(evaluation.content_hash),
      evaluation_id: row.evaluation_id ?? asString(evaluation.evaluation_id),
      previous_evaluation_id: row.previous_evaluation_id ?? asString(evaluation.previous_evaluation_id),
      available_at: row.available_at ?? asString(thesis.available_at),
      origin_mode: row.origin_mode ?? asString(thesis.origin_mode),
      holding_plan_status: row.holding_plan_status ?? holdingNote(evaluation.holding),
      scenario_projection: row.scenario_projection ?? evaluation.scenario_projection as TradeThesisSummary['scenario_projection'],
      chart_lines: row.chart_lines ?? evaluation.chart_lines as ThesisLine[] | undefined,
      family: row.family ?? asString(thesis.family),
      original_rankings: row.original_rankings ?? asRankings(thesis.original_rankings),
      current_rankings: row.current_rankings ?? asRankings(evaluation.current_rankings),
    };
  });
}

function asRankings(value: unknown): RankScope[] | undefined {
  return Array.isArray(value) ? value.filter((item): item is RankScope => Boolean(item) && typeof item === 'object') : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

export function rankText(rank?: RankScope | null): string {
  if (!rank) return '未提供';
  const placement = rank.rank != null ? `${rank.rank}/${rank.universe_size ?? rank.population ?? '?'}` : '未排名';
  return [laneLabel(rank.strategy ?? rank.lane), placement, rank.scope].filter(Boolean).join(' · ');
}

export function thesisChartAnnotations(items: TradeThesisSummary[]): WorkbenchAnnotation[] {
  return items.flatMap((thesis) => {
    const explicit = [...(thesis.chart_lines ?? []), ...originalStructureLines(thesis)];
    return explicit.filter((line) => Number.isFinite(Number(line.price))).map((line, index) => ({
    id: `thesis-${thesis.thesis_id}-${line.id ?? index}`,
    kind: 'price_line' as const,
    label: line.label,
    price: Number(line.price),
    detail: line.detail ?? `${line.kind === 'current_plan' ? '当前有效计划' : '原始结构'}；只展示后端返回的价位`,
    color: line.kind === 'current_plan' ? '#f59e0b' : '#38bdf8',
    source_label: line.source_ref ?? undefined,
    start_date: line.valid_from ?? undefined,
    end_date: line.valid_until ?? undefined,
    }));
  });
}

function originalStructureLines(item: TradeThesisSummary): ThesisLine[] {
  const structure = item.thesis?.original_structure;
  if (!structure || typeof structure !== 'object' || Array.isArray(structure)) return [];
  const record = structure as Record<string, unknown>;
  return (['support', 'reference'] as const).flatMap((key) => {
    const price = record[key];
    if (typeof price !== 'number' || !Number.isFinite(price)) return [];
    return [{
      id: `original-${key}`, price, kind: 'original_structure' as const,
      label: key === 'support' ? '原始结构支撑参考' : '原始结构价位参考',
      detail: '来自冻结的原假设结构；是研究参考位，不是持仓硬止损',
      valid_from: asString(item.thesis?.effective_from), valid_until: asString(item.thesis?.terminal_deadline),
    }];
  });
}

export function textValue(value: string | Record<string, unknown>): string {
  if (typeof value === 'string') return value;
  const metric = typeof value.metric === 'string' ? value.metric : typeof value.condition === 'string' ? value.condition : '';
  const checks: Record<string, string> = {
    close: '价格与原结构的关系待验证',
    full_entry_scenario_confirmed: '完整入场场景未确认，不构成下单信号',
  };
  return String(value.label ?? value.description ?? checks[metric] ?? metricLabelText(metric) ?? JSON.stringify(value));
}

const STATE_LABELS: Record<string, string> = {
  blocked: '约束冲突，不能新增', deferred: '等待下一交易时段', unbound: '未绑定真实计划',
  pending: '待评价', supported: '支持', challenged: '受挑战', invalidated: '已失效', expired: '已到期', superseded: '已替代',
  complete: '完整', partial: '部分', stale: '陈旧', conflict: '冲突', waiting: '等待确认', eligible: '符合场景',
  suspended: '暂停', cancelled: '已取消', unknown: '未知',
};
export const stateLabel = (value?: string | null) => value ? STATE_LABELS[value] ?? value : '未核定';

export function metricLabelText(metric?: string): string {
  const labels: Record<string, string> = {
    amount: '成交额', low: '最低价', close: '收盘价', main_net5: '5日主力净额',
    amount_ratio_previous: '成交额 / 前一交易日', amount_ratio_mean5: '成交额 / 前5日均额',
    amount_vs_previous: '成交额 / 前一日', amount_vs_5d_mean: '成交额 / 5日均额',
    full_entry_scenario_confirmed: '完整入场场景',
  };
  return metric ? labels[metric] ?? metric : '证据';
}
export function benchmarkLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    previous_day: '前一交易日', previous_session: '前一交易日', five_day_mean: '前5日均额',
    previous_5_sessions_mean: '前5个交易日均额', longhuvip_main_net_5_sessions: 'LonghuVIP近5个交易日主力净额',
  };
  return value ? labels[value] ?? value : '未提供';
}

export function observationValue(value: unknown, metric?: string, unit?: string | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return value == null ? '—' : String(value);
  if (unit === 'CNY' && ['amount', 'main_net5'].includes(metric ?? '')) return `${(value / 100_000_000).toFixed(2)} 亿元`;
  if (unit === 'CNY') return `${value.toFixed(2)} 元`;
  if ((metric ?? '').includes('ratio') || unit === 'ratio' || unit === 'x') return `${value.toFixed(2)} 倍`;
  return `${value.toFixed(2)}${unit ? ` ${unit}` : ''}`;
}

export function isEvidenceVersionChange(change: ThesisEvidenceDelta): boolean {
  return change.directly_comparable === false || change.impact === 'evidence_version_changed';
}

export function evidenceVersionNote(change: ThesisEvidenceDelta): string {
  const versions = [change.old_version, change.version].filter(Boolean).join(' → ');
  return `数据口径/版本修正，非行情变化${versions ? `（${versions}）` : ''}`;
}

function laneLabel(value?: string | null): string | undefined {
  if (!value) return undefined;
  const labels: Record<string, string> = {
    accumulation: '潜伏观察', breakout: '放量启动', rotation: '轮动候选', pullback: '回踩承接', trend: '趋势跟踪', event: '事件驱动',
  };
  return labels[value] ?? value;
}

function holdingNote(value: unknown): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  return asString(record.note) ?? asString(record.status);
}

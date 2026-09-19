/** Typed reads of the adapter's /api/research/discipline/* routes (GET only; the browser never calls 5681). */
import { getJson } from '../../api/http';
import type {
  DailyChart, EvaluationsResponse, HistoryResponse, MinuteChart, PlansResponse, ReconciliationsResponse,
} from './types';

const BASE = '/api/research/discipline';
export const ALL_STATUSES = 'active,rejected_by_quality,expired,superseded';
export const DEFAULT_STATUSES = 'active,rejected_by_quality';

export function fetchLatestPlans(accountKey: string, statuses = DEFAULT_STATUSES): Promise<PlansResponse> {
  const params = new URLSearchParams({ account_key: accountKey, status: statuses, limit: '200' });
  return getJson<PlansResponse>(`${BASE}/plans/latest?${params}`);
}

export function fetchDailyChart(planId: string): Promise<DailyChart> {
  return getJson<DailyChart>(`${BASE}/plans/${encodeURIComponent(planId)}/chart?basis=daily`);
}

export function fetchMinuteChart(planId: string, date?: string | null): Promise<MinuteChart> {
  const params = new URLSearchParams({ basis: 'minute' });
  if (date) params.set('date', date);
  return getJson<MinuteChart>(`${BASE}/plans/${encodeURIComponent(planId)}/chart?${params}`);
}

export function fetchEvaluations(planId: string): Promise<EvaluationsResponse> {
  return getJson<EvaluationsResponse>(`${BASE}/plans/${encodeURIComponent(planId)}/evaluations`);
}

export function fetchHistory(accountKey: string, symbol: string): Promise<HistoryResponse> {
  const params = new URLSearchParams({ account_key: accountKey, symbol });
  return getJson<HistoryResponse>(`${BASE}/plans/history?${params}`);
}

export function fetchReconciliations(accountKey: string, options: { symbol?: string; from?: string; to?: string } = {}): Promise<ReconciliationsResponse> {
  const params = new URLSearchParams({ account_key: accountKey });
  if (options.symbol) params.set('symbol', options.symbol);
  if (options.from) params.set('from', options.from);
  if (options.to) params.set('to', options.to);
  return getJson<ReconciliationsResponse>(`${BASE}/reconciliations?${params}`);
}

export const errorText = (cause: unknown) => (cause instanceof Error ? cause.message : String(cause));

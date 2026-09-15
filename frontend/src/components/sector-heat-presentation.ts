import type { SectorHeatHistoryPoint } from '../types/sector-heat';

const keys = ['attention_score', 'activity_score', 'strength_score', 'risk_score'] as const;
export function observedHistory(points: SectorHeatHistoryPoint[], full = false) {
  if (full) return points;
  const hasValue = (point: SectorHeatHistoryPoint) => keys.some((key) => typeof point[key] === 'number' && Number.isFinite(point[key]));
  const start = points.findIndex(hasValue);
  if (start < 0) return [];
  let end = points.length - 1;
  while (end > start && !hasValue(points[end]!)) end--;
  return points.slice(start, end + 1);
}

export function signedPercent(value: unknown) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? `${n > 0 ? '+' : ''}${n.toFixed(2)}%` : '—';
}

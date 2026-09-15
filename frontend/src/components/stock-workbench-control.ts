import type { MetricKey } from './stock-workbench';

export type WorkbenchPanel = 'price' | 'metric' | 'next_session' | 'next_week' | 'messages' | 'trade_plan';
export type WorkbenchAnnotationKind = 'price_line' | 'point' | 'region' | 'note';

export type WorkbenchAnnotation = {
  id: string;
  kind: WorkbenchAnnotationKind;
  label: string;
  detail?: string;
  color?: string;
  source_label?: string;
  as_of?: string;
  price?: number;
  date?: string;
  start_date?: string;
  end_date?: string;
  low?: number;
  high?: number;
};

export type StockWorkbenchControl = {
  contract_version: 'stock-workbench-control.v1';
  revision: number;
  active: boolean;
  workspace_id: string;
  updated_at: string;
  expires_at: string | null;
  symbol: string | null;
  lookback_days: number;
  strategy_key: string | null;
  timeframe: 'daily' | 'weekly';
  metric: MetricKey | null;
  zoom: { start: number; end: number };
  focus: { scope: 'next_session' | 'next_week'; state: string } | null;
  panel_visibility: Record<WorkbenchPanel, boolean>;
  annotations: WorkbenchAnnotation[];
  speaker_note: { title: string; body: string; source_label?: string; as_of?: string } | null;
  presentation_only: true;
};

export function isLiveWorkbenchControl(control: StockWorkbenchControl | null | undefined, now = Date.now()): control is StockWorkbenchControl {
  return Boolean(control?.active && (!control.expires_at || Date.parse(control.expires_at) > now));
}

export function panelIsVisible(control: StockWorkbenchControl | null | undefined, panel: WorkbenchPanel, now = Date.now()): boolean {
  return !isLiveWorkbenchControl(control, now) || control.panel_visibility?.[panel] !== false;
}

export function annotationColor(annotation: WorkbenchAnnotation): string {
  return annotation.color || '#22d3ee';
}

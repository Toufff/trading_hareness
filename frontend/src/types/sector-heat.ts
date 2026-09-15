export type SectorHeatKind = 'industry' | 'concept';

export type SectorHeatFactor = {
  key: string;
  label: string;
  value?: number | null;
  score?: number | null;
  weight?: number | null;
  effective_weight?: number | null;
  contribution?: number | null;
  available?: boolean;
  description?: string;
};

export type SectorHeatHistoryPoint = {
  trade_date?: string;
  phase?: string | null;
  data_as_of?: string | null;
  attention_score?: number | null;
  activity_score?: number | null;
  strength_score?: number | null;
  risk_score?: number | null;
  coverage?: number | null;
  rank?: number | null;
};

export type SectorHeatItem = {
  key: string;
  name: string;
  kind: SectorHeatKind;
  code: string;
  trade_date?: string | null;
  data_as_of?: string | null;
  received_at?: string | null;
  quality?: string | null;
  coverage?: number | null;
  attention_score?: number | null;
  activity_score?: number | null;
  strength_score?: number | null;
  risk_score?: number | null;
  rank?: number | null;
  rank_change?: number | null;
  state_label?: string | null;
  reasons?: string[];
  missing?: string[];
  metrics?: Record<string, number | string | null>;
  factors?: SectorHeatFactor[];
  daily_history?: SectorHeatHistoryPoint[];
  intraday_history?: SectorHeatHistoryPoint[];
  selection?: {
    latest_attempt_at?: string | null;
    latest_attempt_quality?: string | null;
    used_earlier_usable?: boolean;
    reason?: string | null;
  };
};

export type SectorHeatSnapshot = {
  schema_version?: number;
  model_version?: string | null;
  generated_at?: string | null;
  trade_date?: string | null;
  phase?: string | null;
  status?: string | null;
  source?: string | null;
  warnings?: string[];
  summary?: Record<string, unknown>;
  items?: SectorHeatItem[];
};

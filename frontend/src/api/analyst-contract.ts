/** API contract for automated analyst x market reviews.
 * Keep this file free of UI concerns so it can later be replaced by generated
 * OpenAPI types without touching dashboard components.
 */
export type AnalystMarketReview = {
  review_id?: string;
  cadence?: 'daily' | 'weekly';
  period_start?: string;
  period_end?: string;
  status?: string;
  methodology_version?: string;
  summary?: {
    daily_points?: {
      exchange_date: string;
      net_direction_score?: number;
      positive_claims?: number;
      negative_claims?: number;
      market_mean_change_pct?: number | null;
      market_state?: string | null;
      concept_positive_ratio?: number | null;
    }[];
    regressions?: {
      x?: string;
      y?: string;
      status?: string;
      n?: number;
      slope?: number;
      intercept?: number;
      correlation?: number;
      r_squared?: number;
      live_effect?: string;
    }[];
    governance?: { live_effect?: string; notice?: string };
    evaluation?: Record<string, unknown>;
  };
  generated_at?: string;
};

export type AutomationRun = {
  run_id: string;
  task_key: string;
  run_key: string;
  cadence?: string | null;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_class?: string | null;
  error_message?: string | null;
  output_summary?: Record<string, unknown>;
};

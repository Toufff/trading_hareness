import { computed, onMounted, ref } from 'vue';
import { getJson } from '../api/http';

export type TradePlan = {
  plan_key: string;
  plan_kind: 'holding' | 'new_buy';
  symbol: string;
  name: string;
  action: 'hold' | 'observe' | 'buy_on_trigger' | 'reduce_on_trigger' | 'exit_on_trigger' | 'avoid';
  entry_zone?: { lower: string | number; upper: string | number } | null;
  add_trigger?: string | null;
  reduce_trigger?: string | null;
  exit_trigger: string;
  stop_price?: string | number | null;
  target_prices?: Array<string | number>;
  max_position_pct: string | number;
  rationale?: string[];
  risk_flags?: string[];
  valid_until: string;
};

export type HoldingAction = {
  position: {
    symbol: string;
    name: string;
    quantity: string | number;
    sellable_quantity?: string | number;
    average_cost?: string | number | null;
    market_price?: string | number | null;
    market_value?: string | number | null;
    unrealized_pnl?: string | number | null;
    position_weight_pct?: string | number | null;
  };
  plan: TradePlan;
};

export type PersonalDecisionBrief = {
  status: 'ready' | 'partial';
  as_of_at: string;
  market: { status: 'ready' | 'completed' | 'degraded' | 'unavailable'; content?: Record<string, unknown> | null };
  holdings: { status: 'ready' | 'blocked'; portfolio_observed_at?: string | null; actions?: HoldingAction[] };
  new_buys: { status: 'ready'; actions?: TradePlan[] };
  delivery: {
    market_eligible: boolean;
    market_complete?: boolean;
    holding_actions_eligible: boolean;
    new_buy_actions_eligible: boolean;
  };
  diagnostics?: string[];
};

export type DecisionResearchGate = {
  gate_key: string;
  label: string;
  verdict: 'pass' | 'fail' | 'unknown' | 'advisory';
  independent_run: boolean;
  conclusion: string;
};

export type DecisionResearchItem = {
  dossier_key: string;
  as_of_date: string;
  symbol: string;
  name: string;
  strategy_family: string;
  status: 'passed' | 'rejected' | 'incomplete';
  conclusion: string;
  source_candidate_rank?: number | null;
  gates: DecisionResearchGate[];
};

export type DecisionResearchBatch = {
  as_of_date?: string | null;
  summary: { total: number; passed: number; rejected: number; incomplete: number };
  items: DecisionResearchItem[];
  boundary?: string;
};

const DEFAULT_ACCOUNT = 'citics-primary';

export function usePersonalDecisionWorkspace() {
  const accountKey = ref(localStorage.getItem('personal-decision-account') || DEFAULT_ACCOUNT);
  const brief = ref<PersonalDecisionBrief | null>(null);
  const research = ref<DecisionResearchBatch | null>(null);
  const loading = ref(false);
  const error = ref('');
  const researchError = ref('');

  const marketContent = computed(() => brief.value?.market.content ?? {});
  const marketReport = computed(() => {
    const raw = marketContent.value.report;
    return raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
  });

  async function load() {
    loading.value = true;
    error.value = '';
    researchError.value = '';
    try {
      localStorage.setItem('personal-decision-account', accountKey.value);
      const params = new URLSearchParams({ account_key: accountKey.value });
      const [briefResult, researchResult] = await Promise.allSettled([
        getJson<PersonalDecisionBrief>(`/api/research/personal/decision-briefs/latest?${params}`),
        getJson<DecisionResearchBatch>('/api/research/personal/decision-research/latest'),
      ]);
      if (briefResult.status === 'rejected') throw briefResult.reason;
      brief.value = briefResult.value;
      if (researchResult.status === 'fulfilled') research.value = researchResult.value;
      else {
        research.value = null;
        researchError.value = researchResult.reason instanceof Error ? researchResult.reason.message : String(researchResult.reason);
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
      brief.value = null;
      research.value = null;
    } finally {
      loading.value = false;
    }
  }

  onMounted(load);
  return { accountKey, brief, research, loading, error, researchError, marketContent, marketReport, load };
}

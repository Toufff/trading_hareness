import { computed, onMounted, ref } from 'vue';
import { getJson } from '../api/http';
import type { StockWorkbench } from '../components/stock-workbench';

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
  holdings: {
    status: 'ready' | 'blocked'; portfolio_observed_at?: string | null;
    reference_trade_date?: string | null; freshness_status?: 'current' | 'stale_or_unverified';
    age_seconds?: number | null; actions?: HoldingAction[];
  };
  new_buys: { status: 'ready'; actions?: TradePlan[] };
  delivery: {
    market_eligible: boolean;
    market_complete?: boolean;
    holding_actions_eligible: boolean;
    new_buy_actions_eligible: boolean;
  };
  diagnostics?: string[];
};

type MarketAdvice = {
  status: 'ready' | 'completed' | 'degraded' | 'unavailable';
  as_of_at: string;
  content?: Record<string, unknown> | null;
  delivery: { eligible: boolean; complete?: boolean };
  diagnostics?: string[];
};

type HoldingAdvice = PersonalDecisionBrief['holdings'] & {
  as_of_at: string;
  delivery: { eligible: boolean };
  diagnostics?: string[];
};

export type MarketScanWatchItem = {
  symbol: string;
  name: string;
  lane_keys: string[];
  lane_labels: string[];
  tags: Array<{ key: string; label: string; source: 'user' | 'strategy' }>;
  user_requested_tracking: boolean;
  reason?: string | null;
  confirmation?: string | null;
  invalidation?: string | null;
  caution?: string | null;
  sector_label?: string | null;
  review_status: 'retain_watch' | 'downgrade_watch' | 'technical_observation' | 'user_tracking' | 'exclude';
  review_label: string;
  company_conclusion?: string | null;
  company_risk?: string | null;
  business?: string | null;
  tracking_research?: {
    version: string;
    status: 'complete' | 'degraded';
    stance: 'strengthening' | 'mixed_watch' | 'risk_repair';
    headline: string;
    as_of_date?: string | null;
    generated_at?: string | null;
    price_structure?: Record<string, unknown>;
    liquidity?: Record<string, unknown>;
    valuation?: Record<string, unknown>;
    capital_flow?: { windows?: Record<string, Record<string, unknown>>; semantic_boundary?: string | null };
    sector?: Record<string, unknown>;
    events?: Array<{ title?: string | null; verification?: string | null; occurred_at?: string | null; url?: string | null }>;
    conditions?: { confirmation?: string | null; range?: string | null; invalidation?: string | null };
    risks?: string[];
    unavailable_sections?: string[];
    buy_authorized: false;
    depends_on_holdings: false;
  } | null;
  buy_authorized: false;
  depends_on_holdings: false;
};

export type MarketScanWatchlist = {
  as_of_date?: string | null;
  status: 'completed' | 'partial' | 'unavailable';
  research_only: true;
  depends_on_holdings: false;
  total_unique: number;
  strategy_total_unique?: number;
  user_tracking_total?: number;
  items: MarketScanWatchItem[];
  notice?: string;
};

const DEFAULT_ACCOUNT = 'citics-primary';

export type PersonalDecisionScope = 'market' | 'holdings' | 'all';

export function usePersonalDecisionWorkspace(scope: PersonalDecisionScope = 'all') {
  const accountKey = ref(localStorage.getItem('personal-decision-account') || DEFAULT_ACCOUNT);
  const brief = ref<PersonalDecisionBrief | null>(null);
  const formalRecommendation = ref<Record<string, unknown> | null>(null);
  const scanWatchlist = ref<MarketScanWatchlist | null>(null);
  const loading = ref(false);
  const error = ref('');
  const scanError = ref('');
  const marketError = ref('');
  const holdingError = ref('');
  const chartWorkbench = ref<StockWorkbench | null>(null);
  const chartSymbol = ref('');
  const chartOpen = ref(false);
  const chartLoading = ref(false);
  const chartError = ref('');

  const marketContent = computed(() => brief.value?.market.content ?? {});
  const marketReport = computed(() => {
    const raw = marketContent.value.report;
    return raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
  });

  async function load() {
    loading.value = true;
    error.value = '';
    scanError.value = '';
    marketError.value = '';
    holdingError.value = '';
    try {
      const includeMarket = scope !== 'holdings';
      const includeHoldings = scope !== 'market';
      if (includeHoldings) localStorage.setItem('personal-decision-account', accountKey.value);
      const params = new URLSearchParams({ account_key: accountKey.value });
      const now = new Date().toISOString();
      const marketTask: Promise<MarketAdvice> = includeMarket
        ? getJson<MarketAdvice>('/api/research/advice/market/latest')
        : Promise.resolve({ status: 'unavailable', as_of_at: now, content: null, delivery: { eligible: false, complete: false }, diagnostics: [] });
      const holdingTask: Promise<HoldingAdvice> = includeHoldings
        ? getJson<HoldingAdvice>(`/api/research/personal/holding-advice/latest?${params}`)
        : Promise.resolve({ status: 'blocked', as_of_at: now, actions: [], freshness_status: 'stale_or_unverified', delivery: { eligible: false }, diagnostics: [] });
      const recommendationTask: Promise<Record<string, unknown> | null> = includeMarket
        ? getJson<Record<string, unknown>>('/api/research/strategy/post-close/latest')
        : Promise.resolve(null);
      const scanTask: Promise<MarketScanWatchlist | null> = includeMarket
        ? getJson<MarketScanWatchlist>('/api/research/strategy/post-close/watchlist/latest?limit=16')
        : Promise.resolve(null);
      const [marketResult, holdingResult, recommendationResult, scanResult] = await Promise.allSettled([
        marketTask, holdingTask, recommendationTask, scanTask,
      ]);
      const market = marketResult.status === 'fulfilled' ? marketResult.value : {
        status: 'unavailable' as const, as_of_at: new Date().toISOString(), content: null,
        delivery: { eligible: false, complete: false }, diagnostics: ['market_transport_failure'],
      };
      const holdings = holdingResult.status === 'fulfilled' ? holdingResult.value : {
        status: 'blocked' as const, as_of_at: new Date().toISOString(), actions: [],
        freshness_status: 'stale_or_unverified' as const, delivery: { eligible: false },
        diagnostics: ['holding_transport_failure'],
      };
      brief.value = {
        status: (scope === 'holdings' ? holdings.delivery.eligible : market.delivery.complete) ? 'ready' : 'partial',
        as_of_at: scope === 'holdings' ? holdings.as_of_at : market.as_of_at,
        market: { status: market.status, content: market.content },
        holdings,
        new_buys: { status: 'ready', actions: [] },
        delivery: {
          market_eligible: market.delivery.eligible,
          market_complete: market.delivery.complete,
          holding_actions_eligible: holdings.delivery.eligible,
          new_buy_actions_eligible: false,
        },
        diagnostics: [
          ...(includeMarket ? (market.diagnostics ?? []) : []),
          ...(includeHoldings ? (holdings.diagnostics ?? []) : []),
        ],
      };
      if (includeMarket && marketResult.status === 'rejected') marketError.value = marketResult.reason instanceof Error ? marketResult.reason.message : String(marketResult.reason);
      if (includeHoldings && holdingResult.status === 'rejected') holdingError.value = holdingResult.reason instanceof Error ? holdingResult.reason.message : String(holdingResult.reason);
      if (recommendationResult.status === 'fulfilled') {
        const payload = recommendationResult.value as {
          run?: { summary?: { strategy_lanes?: { recommendation_pool?: Record<string, unknown> } } };
          latest_completed?: { summary?: { recommendation_pool?: Record<string, unknown>; strategy_lanes?: { recommendation_pool?: Record<string, unknown> } } };
        } | null;
        formalRecommendation.value = payload?.run?.summary?.strategy_lanes?.recommendation_pool
          ?? payload?.latest_completed?.summary?.recommendation_pool
          ?? payload?.latest_completed?.summary?.strategy_lanes?.recommendation_pool
          ?? null;
      } else formalRecommendation.value = null;
      if (scanResult.status === 'fulfilled') scanWatchlist.value = scanResult.value;
      else {
        scanWatchlist.value = null;
        scanError.value = scanResult.reason instanceof Error ? scanResult.reason.message : String(scanResult.reason);
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
      brief.value = null;
      formalRecommendation.value = null;
      scanWatchlist.value = null;
    } finally {
      loading.value = false;
    }
  }

  function normalizedSymbol(value: string): string {
    const symbol = value.trim().toUpperCase();
    if (/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) return symbol;
    if (!/^\d{6}$/.test(symbol)) throw new Error(`无法识别股票代码：${value}`);
    if (symbol.startsWith('6') || symbol.startsWith('9')) return `${symbol}.SH`;
    if (symbol.startsWith('4') || symbol.startsWith('8')) return `${symbol}.BJ`;
    return `${symbol}.SZ`;
  }

  async function openChart(symbolValue: string) {
    chartLoading.value = true;
    chartError.value = '';
    chartWorkbench.value = null;
    chartOpen.value = true;
    try {
      const symbol = normalizedSymbol(symbolValue);
      chartSymbol.value = symbol;
      chartWorkbench.value = await getJson<StockWorkbench>(`/api/research/stocks/${symbol}/workbench?lookback_days=120`);
    } catch (cause) {
      chartError.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      chartLoading.value = false;
    }
  }

  function closeChart() {
    chartOpen.value = false;
  }

  onMounted(load);
  return {
    accountKey, brief, formalRecommendation, scanWatchlist, loading, error, scanError, marketError, holdingError,
    marketContent, marketReport, load,
    chartWorkbench, chartSymbol, chartOpen, chartLoading, chartError, openChart, closeChart,
  };
}

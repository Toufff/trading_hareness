import type { WorkbenchAnnotation } from './stock-workbench-control';
import { annotationColor } from './stock-workbench-control';

export type WorkbenchBar = {
  date: string; open: number; close: number; high: number; low: number;
  volume?: number | null; amount?: number | null; turnover_rate?: number | null;
  ma5?: number | null; ma10?: number | null; ma20?: number | null; ma60?: number | null;
  dif?: number | null; dea?: number | null; macd?: number | null; rsi14?: number | null;
  k?: number | null; d?: number | null; j?: number | null; atr14?: number | null;
  boll_upper?: number | null; boll_mid?: number | null; boll_lower?: number | null;
};

export type MetricKey = 'vendor_flow' | 'volume' | 'turnover' | 'macd' | 'rsi';

export type WorkbenchStrategy = {
  key: string; label: string; thesis: string; required_panels: string[]; optional_panels: string[];
  overlays: string[]; metrics: MetricKey[]; message_categories: string[]; volume_confirmation: number; horizon_sessions: number;
};

export type WorkbenchScenario = {
  state: string; condition: string; action: string; invalidation?: string; checkpoints?: string[];
};

export type WorkbenchStrategyView = {
  key: string; status: string; blockers: string[]; current_reading: string[];
  next_session: WorkbenchScenario[]; next_week: WorkbenchScenario[];
  levels?: Record<string, number>; probability_status?: string; probability_note?: string;
  regime_route?: { state: string; priority_weight: number; regime: string };
  risk_envelope?: {
    max_single_position_pct: number; max_sector_exposure_pct: number;
    failure_reference: number; failure_rule: string; time_stop: string;
    trailing_rule: string; execution_constraints: string[]; note: string;
  };
};

export type WorkbenchMessage = {
  id: string; category: string; event_type: string; title: string; detail?: string;
  source: string; url?: string | null; occurred_at?: string | null; available_at?: string | null;
  chart_date?: string | null; verification: string; impact: string; relevant_strategies: string[];
};

export type StockWorkbench = {
  contract_version: string; symbol: string; name: string; industry?: string | null; as_of_date: string;
  generated_at: string; strategies: WorkbenchStrategy[]; strategy_views: WorkbenchStrategyView[];
  series: { daily: WorkbenchBar[]; weekly: WorkbenchBar[] }; technical_summary: Record<string, unknown>;
  flow: { status?: string; series?: Record<string, unknown>[]; windows?: Record<string, Record<string, unknown>>; semantic_boundary?: string };
  messages: WorkbenchMessage[]; sectors: Record<string, unknown>[]; market_context: Record<string, unknown>;
  active_trade_plan?: Record<string, unknown> | null; data_health: Record<string, { status: string; detail: string }>;
  artifact_freshness?: Record<string, { status: string; as_of_date?: string | null; latest_available_at?: string | null; detail?: string }>;
  source_health: {
    history?: { status?: string; rows?: number; latest_date?: string | null };
    history_error?: string | null;
    [key: string]: unknown;
  }; notice: string;
};

export function displayPlanValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-';
  if (Array.isArray(value)) return value.map(displayPlanValue).join('、');
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const preferred = ['low', 'high', 'price', 'condition', 'note'];
    const keys = preferred.filter((key) => record[key] !== undefined);
    const selected = keys.length ? keys : Object.keys(record);
    const labels: Record<string, string> = { low: '下沿', high: '上沿', price: '价格', condition: '条件', note: '备注' };
    return selected.map((key) => `${labels[key] ?? key}: ${displayPlanValue(record[key])}`).join('；');
  }
  return String(value);
}

const METRIC_LABELS: Record<MetricKey, string> = {
  vendor_flow: '成交单规模资金', volume: '成交额', turnover: '换手率', macd: 'MACD', rsi: 'RSI',
};

export function metricLabel(metric: MetricKey): string {
  return METRIC_LABELS[metric];
}

export function strategyMetrics(strategy?: WorkbenchStrategy): Array<{ key: MetricKey; label: string }> {
  const keys = (strategy?.metrics?.length ? strategy.metrics : ['volume'] as MetricKey[])
    .filter((key): key is MetricKey => key in METRIC_LABELS);
  return keys.map((key) => ({ key, label: METRIC_LABELS[key] }));
}

export function relevantMessages(messages: WorkbenchMessage[], strategyKey: string): WorkbenchMessage[] {
  return messages.filter((message) => message.relevant_strategies.includes(strategyKey)).slice(0, 20);
}

function finiteNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Turn the persisted account/candidate plan into chart annotations. */
export function tradePlanAnnotations(workbench: StockWorkbench): WorkbenchAnnotation[] {
  const plan = workbench.active_trade_plan;
  const bars = workbench.series.daily;
  if (!plan || !bars.length) return [];
  const start = bars[0]!.date;
  const end = bars[bars.length - 1]!.date;
  const zone = plan.entry_zone && typeof plan.entry_zone === 'object' && !Array.isArray(plan.entry_zone)
    ? plan.entry_zone as Record<string, unknown>
    : {};
  const low = finiteNumber(zone.lower ?? zone.low);
  const high = finiteNumber(zone.upper ?? zone.high);
  const annotations: WorkbenchAnnotation[] = [];
  if (low !== null && high !== null) {
    annotations.push({
      id: 'active-plan-entry-zone', kind: 'region', label: '计划买入区间',
      detail: '来自当前有效交易计划，不是策略自动生成的参考区间', color: '#22c55e',
      start_date: start, end_date: end, low: Math.min(low, high), high: Math.max(low, high),
    });
  }
  const stop = finiteNumber(plan.stop_price);
  if (stop !== null) annotations.push({
    id: 'active-plan-stop', kind: 'price_line', label: '计划止损参考',
    detail: '必须结合计划中的逻辑失效条件执行', color: '#ef4444', price: stop,
  });
  const targets = Array.isArray(plan.target_prices) ? plan.target_prices : [];
  targets.forEach((value, index) => {
    const price = finiteNumber(value);
    if (price !== null) annotations.push({
      id: `active-plan-target-${index + 1}`, kind: 'price_line', label: `计划目标${index + 1}`,
      detail: '来自当前有效交易计划', color: '#a16c2c', price,
    });
  });
  return annotations;
}

function line(name: string, data: Array<number | null | undefined>, color: string, dashed = false) {
  return {
    name, type: 'line', data, showSymbol: false, connectNulls: false, smooth: false,
    lineStyle: { color, width: 1.4, type: dashed ? 'dashed' : 'solid' },
    itemStyle: { color }, animationDurationUpdate: 360,
  };
}

export function priceChartOption(
  bars: WorkbenchBar[], strategy: WorkbenchStrategy | undefined, view: WorkbenchStrategyView | undefined,
  messages: WorkbenchMessage[], zoomStart = 55, zoomEnd = 100, annotations: WorkbenchAnnotation[] = [],
) {
  const dates = bars.map((bar) => bar.date);
  const overlays = new Set(strategy?.overlays ?? []);
  const priceLines = annotations.filter((item) => item.kind === 'price_line' && Number.isFinite(item.price));
  const chartPoints = annotations.filter((item) => item.kind === 'point' && item.date && Number.isFinite(item.price));
  const chartRegions = annotations.filter((item) => item.kind === 'region' && item.start_date && item.end_date && Number.isFinite(item.low) && Number.isFinite(item.high));
  const referenceLines = view?.levels ? [
    { name: '结构支撑', yAxis: view.levels.support, lineStyle: { color: '#2d627c' } },
    { name: '压力参考', yAxis: view.levels.resistance, lineStyle: { color: '#a16c2c' } },
    { name: '失效参考', yAxis: view.levels.failure, lineStyle: { color: '#ae473c' } },
  ].filter((item) => Number.isFinite(item.yAxis)) : [];
  const eventPoints = overlays.has('event_markers') ? messages.filter((message) => message.chart_date && dates.includes(message.chart_date)).slice(-12)
    .map((message) => {
      const index = dates.indexOf(message.chart_date!);
      return { name: message.title, coord: [message.chart_date, bars[index]?.high ?? null], itemStyle: { color: '#79617d' } };
    }) : [];
  const series: Record<string, unknown>[] = [{
    name: '价格', type: 'candlestick',
    data: bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]),
    itemStyle: { color: '#ae473c', color0: '#387b66', borderColor: '#ae473c', borderColor0: '#387b66' },
    animationDurationUpdate: 360,
    markLine: referenceLines.length || priceLines.length ? {
      symbol: ['none', 'none'], label: { formatter: '{b} {c}', position: 'insideEndTop' },
      lineStyle: { type: 'dashed', width: 1 },
      data: [
        ...referenceLines,
        ...priceLines.map((item) => ({
          name: item.label, yAxis: item.price, lineStyle: { color: annotationColor(item), width: 2, type: 'solid' },
          label: { color: annotationColor(item), formatter: `{b} ${item.price}` },
        })),
      ],
    } : undefined,
    markPoint: eventPoints.length || chartPoints.length ? {
      symbol: 'circle', symbolSize: 12, label: { show: false },
      data: [...eventPoints, ...chartPoints.map((item) => ({
        name: item.label, coord: [item.date, item.price], value: item.detail || item.label,
        itemStyle: { color: annotationColor(item), borderColor: '#e2e8f0', borderWidth: 1 },
      }))],
    } : undefined,
    markArea: chartRegions.length ? {
      silent: true,
      label: { color: '#263e48', position: 'insideTopLeft' },
      data: chartRegions.map((item) => ([
        { name: item.label, xAxis: item.start_date, yAxis: item.low, itemStyle: { color: annotationColor(item), opacity: 0.14, borderColor: annotationColor(item), borderWidth: 1 } },
        { xAxis: item.end_date, yAxis: item.high },
      ])),
    } : undefined,
  }];
  if (overlays.has('ma5')) series.push(line('MA5', bars.map((bar) => bar.ma5), '#836026'));
  if (overlays.has('ma10')) series.push(line('MA10', bars.map((bar) => bar.ma10), '#2d627c'));
  if (overlays.has('ma20')) series.push(line('MA20', bars.map((bar) => bar.ma20), '#79617d'));
  if (overlays.has('boll')) {
    series.push(line('布林上轨', bars.map((bar) => bar.boll_upper), '#ae473c', true));
    series.push(line('布林中轨', bars.map((bar) => bar.boll_mid), '#626d6d', true));
    series.push(line('布林下轨', bars.map((bar) => bar.boll_lower), '#387b66', true));
  }
  return {
    animation: true, animationDuration: 260, animationDurationUpdate: 360,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    legend: { top: 2, textStyle: { color: '#626d6d' } },
    grid: { left: 58, right: 68, top: 40, bottom: 58 },
    xAxis: { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#dcd6c5' } }, axisLabel: { color: '#626d6d' } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: '#dcd6c5' } }, axisLabel: { color: '#626d6d' } },
    dataZoom: [{ type: 'inside', start: zoomStart, end: zoomEnd }, { type: 'slider', start: zoomStart, end: zoomEnd, height: 20, bottom: 10, borderColor: 'transparent' }],
    series,
  };
}

export function metricChartOption(bars: WorkbenchBar[], workbench: StockWorkbench, metric: MetricKey) {
  const dates = bars.map((bar) => bar.date);
  const common = {
    animationDurationUpdate: 320, tooltip: { trigger: 'axis' },
    grid: { left: 58, right: 22, top: 30, bottom: 36 },
    xAxis: { type: 'category', data: dates, axisLabel: { color: '#626d6d', hideOverlap: true }, axisLine: { lineStyle: { color: '#dcd6c5' } } },
    yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#dcd6c5' } }, axisLabel: { color: '#626d6d' } },
  };
  if (metric === 'vendor_flow') {
    const byDate = new Map((workbench.flow.series ?? []).map((row) => [String(row.trading_date), Number(row.net_amount)]));
    return { ...common, series: [{ name: '成交单规模净额', type: 'bar', data: dates.map((date) => byDate.get(date) ?? null), itemStyle: { color: (item: { value: number }) => item.value >= 0 ? '#ae473c' : '#387b66' } }] };
  }
  if (metric === 'turnover') return { ...common, yAxis: { ...common.yAxis, name: '%' }, series: [line('换手率', bars.map((bar) => bar.turnover_rate), '#a16c2c')] };
  if (metric === 'macd') return { ...common, series: [
    { name: 'MACD柱', type: 'bar', data: bars.map((bar) => bar.macd), itemStyle: { color: (item: { value: number }) => item.value >= 0 ? '#ae473c' : '#387b66' } },
    line('DIF', bars.map((bar) => bar.dif), '#836026'), line('DEA', bars.map((bar) => bar.dea), '#2d627c'),
  ] };
  if (metric === 'rsi') return { ...common, yAxis: { ...common.yAxis, min: 0, max: 100 }, series: [line('RSI14', bars.map((bar) => bar.rsi14), '#79617d')] };
  return { ...common, series: [{ name: '成交额（亿元）', type: 'bar', data: bars.map((bar) => bar.amount == null ? null : bar.amount / 100_000_000), itemStyle: { color: '#2d627c' } }] };
}

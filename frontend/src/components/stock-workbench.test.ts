import { describe, expect, it } from 'vitest';
import {
  displayPlanValue,
  priceChartOption,
  relevantMessages,
  strategyMetrics,
  tradePlanAnnotations,
  type StockWorkbench,
  type WorkbenchBar,
  type WorkbenchMessage,
  type WorkbenchStrategy,
  type WorkbenchStrategyView,
} from './stock-workbench';
import { isLiveWorkbenchControl, panelIsVisible, type StockWorkbenchControl } from './stock-workbench-control';

const bars: WorkbenchBar[] = [
  { date: '2026-09-03', open: 10, close: 10.2, high: 10.3, low: 9.9, amount: 100_000_000, ma5: 10 },
  { date: '2026-09-04', open: 10.2, close: 10.5, high: 10.6, low: 10.1, amount: 160_000_000, ma5: 10.1 },
];
const strategy: WorkbenchStrategy = {
  key: 'event', label: '事件驱动', thesis: '事件验证', required_panels: ['history', 'messages'],
  optional_panels: ['flow'], overlays: ['ma5', 'event_markers'], metrics: ['volume', 'turnover', 'vendor_flow'], message_categories: ['catalyst'],
  volume_confirmation: 1.3, horizon_sessions: 5,
};
const view: WorkbenchStrategyView = {
  key: 'event', status: 'ready', blockers: [], current_reading: [], next_session: [], next_week: [],
  levels: { support: 9.9, resistance: 10.6, failure: 9.8 },
};
const messages: WorkbenchMessage[] = [
  { id: '1', category: 'catalyst', event_type: 'announcement', title: '订单公告', source: '巨潮', chart_date: '2026-09-04', verification: 'official', impact: '待验证', relevant_strategies: ['event'] },
  { id: '2', category: 'valuation', event_type: 'daily', title: '估值变化', source: '数据库', chart_date: '2026-09-04', verification: 'derived', impact: '中性', relevant_strategies: ['pullback'] },
];

describe('stock strategy workbench', () => {
  it('selects evidence by strategy instead of showing every metric', () => {
    const accumulation = { ...strategy, key: 'accumulation', metrics: ['vendor_flow', 'volume', 'turnover'] as const } as WorkbenchStrategy;
    const trend = { ...strategy, key: 'trend', metrics: ['macd', 'rsi', 'volume'] as const } as WorkbenchStrategy;
    expect(strategyMetrics(accumulation).map((item) => item.key)).toEqual(['vendor_flow', 'volume', 'turnover']);
    expect(strategyMetrics(trend).map((item) => item.key)).toEqual(['macd', 'rsi', 'volume']);
    expect(strategyMetrics(trend).map((item) => item.key)).not.toContain('vendor_flow');
  });

  it('filters strategy messages and annotates only the relevant event', () => {
    const relevant = relevantMessages(messages, 'event');
    expect(relevant.map((item) => item.title)).toEqual(['订单公告']);
    const option = priceChartOption(bars, strategy, view, relevant, 32) as { dataZoom: Array<{ start: number }>; series: Array<Record<string, any>> };
    expect(option.dataZoom[0]?.start).toBe(32);
    expect(option.series[0]?.markPoint.data).toHaveLength(1);
    expect(option.series[0]?.markLine.data.map((item: { name: string }) => item.name)).toEqual(['结构支撑', '压力参考', '失效参考']);
  });

  it('renders temporary agent lines, points and regions without changing the trade plan', () => {
    const option = priceChartOption(bars, strategy, view, [], 10, 80, [
      { id: 'line', kind: 'price_line', label: '观察线', price: 10.4, color: '#22d3ee' },
      { id: 'point', kind: 'point', label: '事件点', date: '2026-09-04', price: 10.5 },
      { id: 'region', kind: 'region', label: '观察区', start_date: '2026-09-03', end_date: '2026-09-04', low: 10, high: 10.6 },
    ]) as { dataZoom: Array<{ start: number; end: number }>; series: Array<Record<string, any>> };
    expect(option.dataZoom[0]).toMatchObject({ start: 10, end: 80 });
    expect(option.series[0]?.markLine.data.map((item: { name: string }) => item.name)).toContain('观察线');
    expect(option.series[0]?.markPoint.data.map((item: { name: string }) => item.name)).toContain('事件点');
    expect(option.series[0]?.markArea.data[0][0].name).toBe('观察区');
  });

  it('expires presentation-only panel controls instead of persisting them as research', () => {
    const control = {
      contract_version: 'stock-workbench-control.v1', revision: 1, active: true, workspace_id: 'primary',
      updated_at: '2026-09-05T02:00:00Z', expires_at: '2026-09-05T02:30:00Z', symbol: '600487.SH',
      lookback_days: 120, strategy_key: 'event', timeframe: 'daily', metric: 'volume', zoom: { start: 20, end: 100 },
      focus: null, panel_visibility: { price: true, metric: true, next_session: true, next_week: true, messages: false, trade_plan: true },
      annotations: [], speaker_note: null, presentation_only: true,
    } satisfies StockWorkbenchControl;
    expect(isLiveWorkbenchControl(control, Date.parse('2026-09-05T02:15:00Z'))).toBe(true);
    expect(panelIsVisible(control, 'messages', Date.parse('2026-09-05T02:15:00Z'))).toBe(false);
    expect(isLiveWorkbenchControl(control, Date.parse('2026-09-05T02:31:00Z'))).toBe(false);
    expect(panelIsVisible(control, 'messages', Date.parse('2026-09-05T02:31:00Z'))).toBe(true);
  });

  it('renders structured trade-plan values for people instead of object placeholders', () => {
    expect(displayPlanValue({ low: 10.2, high: 10.5 })).toBe('下沿: 10.2；上沿: 10.5');
    expect(displayPlanValue(['放量站稳', '板块不弱'])).toBe('放量站稳、板块不弱');
  });

  it('plots the persisted buy zone, stop and targets on the price chart', () => {
    const workbench = {
      series: { daily: bars, weekly: [] },
      active_trade_plan: {
        entry_zone: { lower: 10.1, upper: 10.3 }, stop_price: 9.8, target_prices: [10.8, 11.2],
      },
    } as unknown as StockWorkbench;
    const annotations = tradePlanAnnotations(workbench);
    expect(annotations).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'region', label: '计划买入区间', low: 10.1, high: 10.3 }),
      expect.objectContaining({ kind: 'price_line', label: '计划止损参考', price: 9.8 }),
      expect.objectContaining({ kind: 'price_line', label: '计划目标1', price: 10.8 }),
      expect.objectContaining({ kind: 'price_line', label: '计划目标2', price: 11.2 }),
    ]));
  });

  it('keeps the response contract strategy-oriented', () => {
    const workbench = { strategies: [strategy], strategy_views: [view] } as StockWorkbench;
    expect(workbench.strategies[0]?.required_panels).toEqual(['history', 'messages']);
    expect(workbench.strategy_views[0]?.next_week).toEqual([]);
  });
});

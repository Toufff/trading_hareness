/** ECharts options for the discipline chart (daily with lines / minute with VWAP). Pure. */
import type { EChartsOption } from 'echarts';
import type { AxisSlot, MergedLine } from './discipline-model';
import { KIND_LABEL, LINE_STYLE, clipSegment } from './discipline-model';
import type { ChartBar, DailyChart, EvaluationTransition, LadderStep, MinuteRow, TradeFill } from './types';

export const UP_COLOR = '#e15a5a';
export const DOWN_COLOR = '#1bad86';
export const GRID_LEFT = 58;
export const GRID_RIGHT = 196;

export type ChartLayers = { ma: boolean; atr: boolean; ladder: boolean; evaluations: boolean; trades: boolean };
export const DEFAULT_LAYERS: ChartLayers = { ma: true, atr: false, ladder: true, evaluations: true, trades: true };

export type DailyOptionInput = {
  chart: DailyChart;
  axis: AxisSlot[];
  lines: MergedLine[];
  planDate: string;
  validUntil: string;
  highlightKey?: string | null;
  focusPrice?: number | null;
  layers: ChartLayers;
  deadline?: { date: string; label: string } | null;
  confirmKeyEndsAtDeadline?: string | null;
  trail?: { armPrice: number; target: number; triggeredOn: string | null } | null;
  buyBand?: { low: number; high: number } | null;
  referencePrice?: number | null;
  costPrice?: number | null;
  ladder?: LadderStep[];
  transitions?: EvaluationTransition[];
  trades?: TradeFill[];
  /** Right gutter for the price tags; narrower on a phone. */
  gridRight?: number;
};

const fmt = (value: number | null | undefined, digits = 2) => (value === null || value === undefined ? '—' : value.toFixed(digits));
const shortDate = (day: string) => day.slice(5);

export function amountText(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  return `${(value / 1e4).toFixed(0)}万`;
}

export function barTooltip(bar: ChartBar | undefined, slot: AxisSlot | undefined): string {
  if (!slot) return '';
  if (!bar) return `${slot.date}<br/>${slot.kind === 'closed' ? '休市' : '有效期内未来交易日'}`;
  const change = bar.change_pct === null ? '—' : `${bar.change_pct >= 0 ? '+' : ''}${bar.change_pct.toFixed(2)}%`;
  return [
    `<b>${bar.date}</b>`,
    `开 ${fmt(bar.open)}　高 ${fmt(bar.high)}`,
    `低 ${fmt(bar.low)}　收 ${fmt(bar.close)}（${change}）`,
    `成交额 ${amountText(bar.amount)}`,
    `MA5 ${fmt(bar.ma5)}　MA10 ${fmt(bar.ma10)}　MA20 ${fmt(bar.ma20)}`,
    `ATR14 ${fmt(bar.atr14)}`,
  ].join('<br/>');
}

function segmentStyle(line: MergedLine, highlightKey: string | null | undefined) {
  const style = LINE_STYLE[line.style];
  const faded = Boolean(highlightKey) && highlightKey !== line.key;
  return {
    color: style.color, type: style.type, width: highlightKey === line.key ? style.width * 2 : style.width,
    opacity: faded ? 0.25 : 1,
  };
}

/** Segments of the discipline lines, clipped to [plan date, valid_until] (a confirm line may end at its deadline). */
export function lineSegments(input: Pick<DailyOptionInput, 'axis' | 'lines' | 'planDate' | 'validUntil' | 'highlightKey' | 'deadline' | 'confirmKeyEndsAtDeadline'>) {
  const out: Array<[Record<string, unknown>, Record<string, unknown>]> = [];
  for (const line of input.lines) {
    const end = input.deadline && line.key === input.confirmKeyEndsAtDeadline ? input.deadline.date : input.validUntil;
    const range = clipSegment(input.axis, input.planDate, end);
    if (!range) continue;
    const [start, stop] = range;
    out.push([
      { name: line.text, lineKey: line.key, coord: [input.axis[start]!.date, line.price], lineStyle: segmentStyle(line, input.highlightKey) },
      { coord: [input.axis[stop]!.date, line.price] },
    ]);
  }
  return out;
}

export function buildDailyOption(input: DailyOptionInput): EChartsOption {
  const { chart, axis, layers } = input;
  const bars = new Map(chart.bars.map((bar) => [bar.date, bar]));
  const dates = axis.map((slot) => slot.date);
  const candle = axis.map((slot) => {
    const bar = bars.get(slot.date);
    return bar ? [bar.open, bar.close, bar.low, bar.high] : '-';
  });
  const volume = axis.map((slot) => {
    const bar = bars.get(slot.date);
    return bar ? { value: bar.volume ?? 0, itemStyle: { color: bar.close >= bar.open ? UP_COLOR : DOWN_COLOR } } : '-';
  });
  const markLineData: Array<unknown> = lineSegments(input);
  if (input.referencePrice !== null && input.referencePrice !== undefined) {
    const range = clipSegment(axis, input.planDate, input.validUntil);
    if (range) markLineData.push([
      { name: `参考价 ${fmt(input.referencePrice)}`, lineKey: 'reference', coord: [dates[range[0]], input.referencePrice],
        lineStyle: { ...LINE_STYLE.reference, opacity: input.highlightKey ? 0.35 : 0.9 } },
      { coord: [dates[range[1]], input.referencePrice] }]);
  }
  if (input.costPrice !== null && input.costPrice !== undefined) {
    const range = clipSegment(axis, input.planDate, input.validUntil);
    if (range) markLineData.push([
      { name: `成本 ${fmt(input.costPrice)}`, lineKey: 'cost', coord: [dates[range[0]], input.costPrice],
        lineStyle: { ...LINE_STYLE.cost, opacity: input.highlightKey ? 0.35 : 0.9 } },
      { coord: [dates[range[1]], input.costPrice] }]);
  }
  if (input.trail) {
    const range = clipSegment(axis, input.planDate, input.validUntil);
    if (range) {
      const endDate = dates[range[1]];
      markLineData.push([
        { name: `上移到 ${fmt(input.trail.target)}`, lineKey: 'trail-arrow', coord: [endDate, input.trail.armPrice],
          symbol: 'none', lineStyle: { color: LINE_STYLE.trail.color, type: 'dashed', width: 1.2 } },
        { coord: [endDate, input.trail.target], symbol: 'arrow', symbolSize: 9 }]);
      if (input.trail.triggeredOn) {
        const moved = clipSegment(axis, input.trail.triggeredOn, input.validUntil);
        if (moved) markLineData.push([
          { name: `移动止损 ${fmt(input.trail.target)}（已上移）`, lineKey: 'trail-moved', coord: [dates[moved[0]], input.trail.target],
            lineStyle: { color: LINE_STYLE.trail.color, type: 'solid', width: 2 } },
          { coord: [dates[moved[1]], input.trail.target] }]);
      }
    }
  }
  if (input.deadline && dates.includes(input.deadline.date)) {
    markLineData.push({
      name: input.deadline.label, xAxis: input.deadline.date, lineKey: 'deadline',
      lineStyle: { color: '#8a94a6', type: 'dashed', width: 1.2 },
      label: { show: true, formatter: input.deadline.label, position: 'insideEndTop', color: '#5b6474', fontSize: 11 },
    });
  }

  const markAreas: Array<unknown> = [];
  for (const closure of chart.closures) {
    const inAxis = closure.dates.filter((day) => dates.includes(day));
    if (!inAxis.length) continue;
    markAreas.push([
      { name: closure.label || '休市', xAxis: inAxis[0], itemStyle: { color: 'rgba(148,163,184,0.22)' },
        label: { show: true, position: 'insideTop', color: '#64748b', fontSize: 11 } },
      { xAxis: inAxis[inAxis.length - 1] }]);
  }
  if (input.buyBand) {
    const range = clipSegment(axis, input.planDate, input.validUntil);
    if (range) markAreas.push([
      { name: '可买区间', xAxis: dates[range[0]], yAxis: input.buyBand.low, itemStyle: { color: 'rgba(127,209,164,0.16)' },
        label: { show: false } },
      { xAxis: dates[range[1]], yAxis: input.buyBand.high }]);
  }

  const markPoints: Array<unknown> = [];
  for (const point of chart.structure_points) {
    if (!point.date || !bars.has(point.date)) continue;
    markPoints.push({ name: point.label, coord: [point.date, point.price], symbol: 'triangle', symbolSize: 9,
      symbolOffset: [0, 8], itemStyle: { color: '#475569' },
      label: { show: true, formatter: point.label, position: 'bottom', color: '#334155', fontSize: 10, distance: 8 } });
  }
  if (layers.evaluations) {
    for (const item of input.transitions ?? []) {
      const day = item.date ?? item.trading_date;
      if (!day || !(item.to === 'triggered' || item.to === 'capped')) continue;
      const bar = bars.get(day);
      if (!bar) continue;
      const up = item.kind === 'trigger' || item.kind === 'trail';
      markPoints.push({
        name: `${KIND_LABEL[item.kind] ?? item.kind}${item.to === 'capped' ? '越过追高上限' : '触发'}`,
        coord: [day, up ? bar.low : bar.high], symbol: up ? 'triangle' : 'pin', symbolRotate: up ? 0 : 180,
        symbolSize: 14, symbolOffset: [0, up ? 12 : -12], itemStyle: { color: up ? '#1a9e5a' : '#d93026' },
        label: { show: true, formatter: `${up ? '▲' : '▼'}${KIND_LABEL[item.kind] ?? item.kind}`, position: up ? 'bottom' : 'top', fontSize: 10,
          color: up ? '#166534' : '#991b1b' },
      });
    }
  }
  if (layers.trades) {
    for (const trade of input.trades ?? []) {
      const price = Number(trade.price);
      if (!dates.includes(trade.trade_date) || !Number.isFinite(price)) continue;
      const buy = trade.side === 'buy';
      const verdict = trade.verdicts.map((item) => item.verdict).join('、') || '未对账';
      markPoints.push({
        name: `${buy ? '买' : '卖'} ${trade.quantity} 股 @ ${price.toFixed(2)}（${verdict}）`,
        coord: [trade.trade_date, price], symbol: 'circle', symbolSize: 16,
        itemStyle: { color: buy ? '#dc2626' : '#15803d', borderColor: '#fff', borderWidth: 1 },
        label: { show: true, formatter: buy ? 'B' : 'S', color: '#fff', fontSize: 10, fontWeight: 'bold' },
        tradeRecordId: trade.record_id, verdict,
      });
    }
  }

  const series: Array<Record<string, unknown>> = [
    {
      name: 'K线', type: 'candlestick', data: candle, xAxisIndex: 0, yAxisIndex: 0, barMaxWidth: 12,
      itemStyle: { color: UP_COLOR, color0: DOWN_COLOR, borderColor: UP_COLOR, borderColor0: DOWN_COLOR },
      markLine: { silent: false, symbol: ['none', 'none'], label: { show: false }, emphasis: { lineStyle: { width: 3 } },
        tooltip: { show: false }, data: markLineData, animation: false },
      markArea: { silent: true, data: markAreas },
      markPoint: { data: markPoints, animation: false },
    },
    {
      name: '成交量', type: 'bar', data: volume, xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 12,
    },
  ];
  if (layers.ma) {
    const ma = (key: 'ma5' | 'ma10' | 'ma20', color: string) => ({
      name: `${key.toUpperCase()}（状态，非止损）`, type: 'line', symbol: 'none', smooth: false, xAxisIndex: 0, yAxisIndex: 0,
      data: axis.map((slot) => bars.get(slot.date)?.[key] ?? '-'), lineStyle: { width: 1, color, opacity: 0.8 },
      itemStyle: { color }, z: 1, silent: true,
    });
    series.push(ma('ma5', '#f59e0b'), ma('ma10', '#6366f1'), ma('ma20', '#a855f7'));
  }
  if (layers.atr) {
    for (const [key, label] of [['atr_upper', 'ATR 带上沿'], ['atr_lower', 'ATR 带下沿']] as const) {
      series.push({
        name: label, type: 'line', symbol: 'none', xAxisIndex: 0, yAxisIndex: 0, silent: true,
        data: axis.map((slot) => bars.get(slot.date)?.[key] ?? '-'),
        lineStyle: { width: 1, type: 'dashed', color: '#94a3b8' }, itemStyle: { color: '#94a3b8' },
      });
    }
  }
  if (layers.ladder && input.ladder?.length) {
    const values = axis.map((slot) => {
      const step = [...input.ladder!].reverse().find((item) => item.trading_date <= slot.date && slot.date <= item.until);
      return step?.hard_stop ?? '-';
    });
    const lowered = input.ladder.filter((step) => step.lowered && step.hard_stop !== null && dates.includes(step.trading_date));
    series.push({
      name: '止损阶梯', type: 'line', step: 'end', symbol: 'none', xAxisIndex: 0, yAxisIndex: 0, data: values,
      lineStyle: { width: 1.2, color: '#b91c1c', opacity: 0.45 }, itemStyle: { color: '#b91c1c' }, silent: true,
      markPoint: { data: lowered.map((step) => ({
        name: `止损下移 ${fmt(step.previous_hard_stop)}→${fmt(step.hard_stop)}：${step.lowered_reason || '未记录理由'}`,
        coord: [step.trading_date, step.hard_stop], symbol: 'diamond', symbolSize: 11, itemStyle: { color: '#eab308' },
        label: { show: true, formatter: '下移', color: '#854d0e', fontSize: 10, position: 'bottom' } })) },
    });
  }

  const focus = input.focusPrice ?? null;
  const visibleStart = Math.max(0, Math.round(((axis.length - 32) / Math.max(axis.length, 1)) * 100));
  const right = input.gridRight ?? GRID_RIGHT;
  return {
    animation: false,
    grid: [
      { left: GRID_LEFT, right, top: 30, bottom: '30%' },
      { left: GRID_LEFT, right, top: '75%', bottom: 42 },
    ],
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross', link: [{ xAxisIndex: 'all' }] }, confine: true,
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params as Array<{ dataIndex: number }> : [params as { dataIndex: number }];
        const index = list[0]?.dataIndex ?? 0;
        const slot = axis[index];
        return barTooltip(slot ? bars.get(slot.date) : undefined, slot);
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, gridIndex: 0, axisLabel: { formatter: shortDate, color: '#64748b' },
        axisLine: { lineStyle: { color: '#cbd5e1' } } },
      { type: 'category', data: dates, boundaryGap: true, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false },
        axisLine: { lineStyle: { color: '#cbd5e1' } } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, position: 'left', splitLine: { lineStyle: { color: 'rgba(148,163,184,.18)' } },
        axisLabel: { color: '#64748b' },
        min: focus === null ? undefined : (value: { min: number }) => Math.min(value.min, focus) * 0.985,
        max: focus === null ? undefined : (value: { max: number }) => Math.max(value.max, focus) * 1.015 },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: visibleStart, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], start: visibleStart, end: 100, height: 18, bottom: 8 },
    ],
    series: series as EChartsOption['series'],
  };
}

export type MinuteOptionInput = { rows: MinuteRow[]; hardStop: number | null; closeOnly?: boolean };

export function buildMinuteOption(input: MinuteOptionInput): EChartsOption {
  const times = input.rows.map((row) => row.time);
  const stop = input.hardStop;
  const hardStopLine = input.hardStop === null ? undefined : {
    symbol: ['none', 'none'], animation: false,
    data: [{ name: `硬止损盘中版 ${fmt(input.hardStop)}`, yAxis: input.hardStop,
      lineStyle: { color: LINE_STYLE.hard.color, width: 2.4, type: 'solid' },
      label: { formatter: `硬止损 ${fmt(input.hardStop)} · 连续3根分钟收盘跌破`, position: 'insideEndTop', color: LINE_STYLE.hard.color } }],
  };
  // The Longhu trend feed has one price per minute: draw it as a price line instead of inventing candles.
  const price = input.closeOnly
    ? { name: '分钟收盘', type: 'line', symbol: 'none', data: input.rows.map((row) => row.close),
      lineStyle: { color: '#1f2937', width: 1.4 }, markLine: hardStopLine }
    : { name: '1分钟K', type: 'candlestick', data: input.rows.map((row) => [row.open, row.close, row.low, row.high]),
      itemStyle: { color: UP_COLOR, color0: DOWN_COLOR, borderColor: UP_COLOR, borderColor0: DOWN_COLOR }, markLine: hardStopLine };
  return {
    animation: false,
    grid: [
      { left: GRID_LEFT, right: 110, top: 30, bottom: '30%' },
      { left: GRID_LEFT, right: 110, top: '75%', bottom: 30 },
    ],
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' }, confine: true,
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params as Array<{ dataIndex: number }> : [params as { dataIndex: number }];
        const row = input.rows[list[0]?.dataIndex ?? 0];
        if (!row) return '';
        const ohlc = row.open === null ? `收 ${fmt(row.close)}` : `开 ${fmt(row.open)} 高 ${fmt(row.high)}<br/>低 ${fmt(row.low)} 收 ${fmt(row.close)}`;
        return [`<b>${row.time}</b>`, ohlc, `VWAP ${fmt(row.vwap, 3)}`, `成交额 ${amountText(row.amount)}`].join('<br/>');
      },
    },
    xAxis: [
      { type: 'category', data: times, gridIndex: 0, axisLabel: { color: '#64748b' } },
      { type: 'category', data: times, gridIndex: 1, axisLabel: { show: false } },
    ],
    yAxis: [
      // keep the intraday hard stop on screen even when the session never came near it
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: 'rgba(148,163,184,.18)' } },
        min: stop === null ? undefined : (value: { min: number }) => Math.min(value.min, stop) * 0.995 },
      { scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      price,
      { name: 'VWAP', type: 'line', symbol: 'none', data: input.rows.map((row) => row.vwap ?? '-'), lineStyle: { color: '#f59e0b', width: 1.4 } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: input.rows.map((row, index) => {
        const previous = index > 0 ? input.rows[index - 1]!.close : row.open ?? row.close;
        return { value: row.volume ?? 0, itemStyle: { color: row.close >= previous ? UP_COLOR : DOWN_COLOR } };
      }) },
    ] as EChartsOption['series'],
  };
}

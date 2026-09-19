import { describe, expect, it } from 'vitest';
import plansFixture from '../../../e2e/fixtures/discipline/plans-latest.json';
import chart600613 from '../../../e2e/fixtures/discipline/chart-600613.SH.json';
import minute600613 from '../../../e2e/fixtures/discipline/minute-600613.SH-2026-09-18.json';
import reconciliations from '../../../e2e/fixtures/discipline/reconciliations.json';
import { DEFAULT_LAYERS, barTooltip, buildDailyOption, buildMinuteOption, lineSegments } from './discipline-chart-option';
import { buildAxis, mergeLines, priceLines } from './discipline-model';
import type { DailyChart, DisciplinePlan, MinuteChart, TradeFill } from './types';

const plan = (plansFixture.items as unknown as DisciplinePlan[]).find((item) => item.symbol === '600613.SH')!;
const chart = chart600613 as unknown as DailyChart;
const axis = buildAxis(chart);
const lines = mergeLines(priceLines(plan));
const base = { chart, axis, lines, planDate: '2026-09-18', validUntil: '2026-09-28', layers: { ...DEFAULT_LAYERS } };

type Series = { name: string; type: string; data: unknown[]; markLine?: { data: unknown[] }; markArea?: { data: unknown[] }; markPoint?: { data: Array<{ name: string; coord?: unknown[] }> } };
const seriesOf = (option: ReturnType<typeof buildDailyOption>) => option.series as unknown as Series[];

describe('daily discipline option', () => {
  it('draws every line only from the plan date to valid_until, the confirm line only to its T+3 deadline', () => {
    const confirmKey = lines.find((line) => line.kinds.includes('time_stop'))!.key;
    const segments = lineSegments({ ...base, deadline: { date: '2026-09-23', label: 'T+3' }, confirmKeyEndsAtDeadline: confirmKey });
    for (const [start, end] of segments) {
      expect((start.coord as unknown[])[0]).toBe('2026-09-18');
      expect(['2026-09-28', '2026-09-23']).toContain((end.coord as unknown[])[0]);
    }
    const confirm = segments.find(([start]) => start.lineKey === confirmKey)!;
    expect((confirm[1].coord as unknown[])[0]).toBe('2026-09-23');
  });

  it('highlights the hovered line and fades the others', () => {
    const hard = lines.find((line) => line.kinds.includes('hard_stop'))!;
    const segments = lineSegments({ ...base, highlightKey: hard.key });
    const style = (key: string) => segments.find(([start]) => start.lineKey === key)![0].lineStyle as { width: number; opacity: number };
    expect(style(hard.key).width).toBeCloseTo(5.2);
    expect(style(hard.key).opacity).toBe(1);
    expect(style(lines.find((line) => line.key !== hard.key)!.key).opacity).toBe(0.25);
  });

  it('shades the closure, marks low20 on its bar and the real fills as B/S', () => {
    const option = buildDailyOption({ ...base, trades: (reconciliations.trades as unknown as TradeFill[]).filter((trade) => trade.symbol === '600613.SH'),
      deadline: { date: '2026-09-23', label: 'T+3 收盘未站回 8.69 → 退出' } });
    const candle = seriesOf(option)[0]!;
    expect(candle.type).toBe('candlestick');
    expect(candle.data).toHaveLength(axis.length);
    expect(candle.data[candle.data.length - 1]).toBe('-');           // future session slot
    expect(JSON.stringify(candle.markArea!.data)).toContain('休市');
    const points = candle.markPoint!.data;
    expect(points.find((point) => point.name === 'low20 7.92')!.coord).toEqual(['2026-09-14', 7.92]);
    // the four real buys of 09-18 are ONE marker under the candle, not four circles stacked on the body
    const fills = points.filter((point) => (point as { markerKind?: string }).markerKind === 'fill');
    const buys0918 = fills.filter((point) => point.coord?.[0] === '2026-09-18');
    expect(buys0918).toHaveLength(1);
    const marker = buys0918[0] as unknown as { name: string; coord: [string, number]; label: { formatter: string }; detail: string };
    expect(marker.label.formatter).toBe('B×4');
    expect(marker.coord[1]).toBe(8.12);                                  // the 09-18 low: outside the candle body
    expect(marker.name).toBe('买入 4 笔 5800 股（尚未对账）');
    expect(marker.detail.split(String.fromCharCode(10))).toHaveLength(5);
    expect(marker.detail).toContain('09:49 买 1500 股 @ 8.37（尚未对账）');
    // every marker explains itself
    for (const point of points) expect(typeof (point as { detail?: unknown }).detail).toBe('string');
    expect(JSON.stringify(candle.markLine!.data)).toContain('T+3 收盘未站回 8.69 → 退出');
  });

  it('keeps the moving averages as labelled state lines and hides the ATR band by default', () => {
    const names = seriesOf(buildDailyOption(base)).map((series) => series.name);
    expect(names).toContain('MA5（状态，非止损）');
    expect(names).not.toContain('ATR 带上沿');
    const withAtr = seriesOf(buildDailyOption({ ...base, layers: { ...DEFAULT_LAYERS, atr: true, ma: false } })).map((series) => series.name);
    expect(withAtr).toContain('ATR 带上沿');
    expect(withAtr).not.toContain('MA5（状态，非止损）');
  });

  it('marks a lowered stop on the ladder in yellow with its reason', () => {
    const ladder = [
      { plan_id: 'a', plan_key: 'a', status: 'superseded', plan_kind: 'holding', trading_date: '2026-09-17', until: '2026-09-18', hard_stop: 7.8, previous_hard_stop: null, lowered: false, lowered_reason: null, supersedes_plan_id: null },
      { plan_id: 'b', plan_key: 'b', status: 'active', plan_kind: 'holding', trading_date: '2026-09-18', until: '2026-09-28', hard_stop: 7.63, previous_hard_stop: 7.8, lowered: true, lowered_reason: '急跌低点下移', supersedes_plan_id: 'a' },
    ];
    const ladderSeries = seriesOf(buildDailyOption({ ...base, ladder })).find((series) => series.name === '止损阶梯')!;
    expect(ladderSeries.markPoint!.data[0]!.name).toBe('止损下移 7.80→7.63：急跌低点下移');
  });

  it('tooltip carries OHLC, change, amount, MAs and ATR', () => {
    const bar = chart.bars[chart.bars.length - 1]!;
    const text = barTooltip(bar, { date: bar.date, kind: 'bar' });
    for (const part of ['2026-09-18', '收 8.41', 'MA5 8.38', 'ATR14 0.86', '成交额']) expect(text).toContain(part);
    expect(barTooltip(undefined, { date: '2026-09-25', kind: 'closed' })).toContain('休市');
  });
});

describe('minute option', () => {
  it('draws the Longhu close-only tape as a line with VWAP and the intraday hard stop', () => {
    const minute = minute600613 as unknown as MinuteChart;
    expect(minute.bar_type).toBe('close_only');
    const option = buildMinuteOption({ rows: minute.rows, hardStop: 7.63, closeOnly: true });
    const series = option.series as unknown as Series[];
    expect(series[0]!.type).toBe('line');
    expect(series.map((item) => item.name)).toContain('VWAP');
    expect(JSON.stringify(series[0]!.markLine)).toContain('硬止损 7.63');
    expect(series[0]!.data).toHaveLength(minute.rows.length);
  });
});

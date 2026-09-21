import { describe, expect, it } from 'vitest';
import plansFixture from '../../../e2e/fixtures/discipline/plans-latest.json';
import chart600613 from '../../../e2e/fixtures/discipline/chart-600613.SH.json';
import {
  buildAxis, capSummary, clipSegment, defaultVisibleKinds, distance, effectiveStatus, lastClosedSession, lineTag, mergeLines,
  nearestPendingLine, priceLines, snapshotWarnings, staggerLabels, timeStopDeadline, todayAction, totalRiskPct,
  trailingBelow, validityRemaining,
} from './discipline-model';
import type { DailyChart, DisciplinePlan, EvaluationsResponse, TradeFill } from './types';

const plans = plansFixture.items as unknown as DisciplinePlan[];
const plan = (symbol: string) => plans.find((item) => item.symbol === symbol)!;
const chart = chart600613 as unknown as DailyChart;

describe('line merging', () => {
  it('merges same-price lines into one line with several labels (600613: no_add and time_stop at 8.69)', () => {
    const merged = mergeLines(priceLines(plan('600613.SH')));
    const confirm = merged.find((line) => line.price === 8.69)!;
    expect(confirm.kinds).toEqual(['no_add', 'time_stop']);
    expect(confirm.labels).toHaveLength(2);
    expect(confirm.text).toBe('禁加仓 / 时间止损 8.69');
    // the daily hard stop and its intraday copy are one line on the chart
    const hard = merged.find((line) => line.kinds.includes('hard_stop'))!;
    expect(hard.ids).toHaveLength(2);
    expect(hard.price).toBe(7.63);
    expect(lineTag(hard)).toBe('硬止损 7.63 · 收盘跌破全部退出');
    // sorted top-down by price
    expect(merged.map((line) => line.price)).toEqual([...merged.map((line) => line.price)].sort((a, b) => b - a));
  });

  it('time-only lines (exposure, holiday) are not drawn as levels', () => {
    expect(priceLines(plan('600613.SH')).some((line) => line.kind === 'exposure')).toBe(false);
  });
});

describe('label staggering', () => {
  it('keeps order and a minimum gap, and stays inside the plot', () => {
    const out = staggerLabels([100, 102, 104, 300], 17, 30, 320);
    expect(out[1]! - out[0]!).toBeGreaterThanOrEqual(17);
    expect(out[2]! - out[1]!).toBeGreaterThanOrEqual(17);
    expect(out[3]).toBe(300);
    expect(out[0]).toBe(100);
  });
  it('pushes a crowded bottom back up instead of leaving the plot', () => {
    const out = staggerLabels([318, 319, 320], 17, 30, 320);
    expect(Math.max(...out)).toBeLessThanOrEqual(320);
    expect(out[2]! - out[1]!).toBeGreaterThanOrEqual(17);
  });
  it('returns positions in input order', () => {
    const out = staggerLabels([200, 100], 17, 0, 400);
    expect(out).toEqual([200, 100]);
  });
});

describe('axis and segment clipping', () => {
  const axis = buildAxis(chart);
  it('extends to valid_until with future slots and shades the Mid-Autumn closure', () => {
    expect(axis[axis.length - 1]).toEqual({ date: '2026-09-28', kind: 'future' });
    expect(axis.filter((slot) => slot.kind === 'closed').map((slot) => slot.date)).toEqual(['2026-09-25', '2026-09-26', '2026-09-27']);
    expect(axis.find((slot) => slot.date === '2026-09-18')?.kind).toBe('bar');
  });
  it('clips a line to [plan date, valid_until]', () => {
    const range = clipSegment(axis, '2026-09-18', '2026-09-28')!;
    expect(axis[range[0]]!.date).toBe('2026-09-18');
    expect(axis[range[1]]!.date).toBe('2026-09-28');
    expect(clipSegment(axis, '2026-10-10', '2026-10-12')).toBeNull();
    expect(clipSegment(axis, '2026-09-28', '2026-09-18')).toBeNull();
  });
  it('places the T+3 time stop on the third session after the plan date', () => {
    expect(timeStopDeadline(plan('600613.SH'), chart.sessions)).toBe('2026-09-23');
  });
});

describe('distances', () => {
  it('measures percent and ATR multiples from the current price', () => {
    const gap = distance(8.41, 7.63, 0.8564)!;
    expect(gap.pct).toBeCloseTo(9.27, 1);
    expect(gap.atr).toBeCloseTo(0.91, 2);
    expect(gap.text).toBe('9.3% / 0.9×ATR');
    expect(distance(8.41, 9.35, null)!.text).toBe('+11.2%');
    expect(distance(null, 7.63, 1)).toBeNull();
  });
});

describe('today action wording', () => {
  it('reduces an over-cap crash_rebound holding at the next open (600613)', () => {
    const action = todayAction(plan('600613.SH'), null, '2026-09-19');
    expect(action.headline).toBe('09-21 开盘15分钟内减到 1200 股（卖 4600，可卖 5800）');
    expect(action.short).toBe('减到1200股');
    expect(action.timing).toBe('09-21 09:30–09:45');
  });
  it('clears a broken holding whose stage target is zero (000977)', () => {
    expect(todayAction(plan('000977.SZ'), null, '2026-09-19').headline).toBe('09-21 开盘15分钟内清仓 300 股（可卖 300）');
  });
  it('a new buy waits for its trigger under the chase cap', () => {
    const action = todayAction(plan('000811.SZ'), null, '2026-09-19');
    expect(action.headline).toBe('等待触发：收盘在 39.68–42.74 之间且成交额不低于前一日且行业当日不走弱，最多买 300 股');
    expect([...defaultVisibleKinds(plan('000811.SZ'), action)]).toEqual(expect.arrayContaining(['trigger', 'chase_cap', 'cancel', 'hard_stop']));
  });
  it('a capped trigger says do not buy', () => {
    const p = plan('000811.SZ');
    const index = p.lines.findIndex((line) => line.kind === 'trigger');
    const evaluations = { lines: [{ index, kind: 'trigger', label: '', basis: 'daily', states: [{ basis: 'daily', state: 'capped', trading_date: '2026-09-21', as_of_at: null, triggered_at: null, trigger_price: 43 }] }],
      transitions: [], plan_state_changes: [], evaluations: [], count: 1, plan_id: p.plan_id } as unknown as EvaluationsResponse;
    expect(todayAction(p, evaluations, '2026-09-21').headline).toBe('已越过追高上限，不买');
  });
  it('an exit signal overrides everything else', () => {
    const p = plan('600613.SH');
    const evaluations = { lines: [], transitions: [], evaluations: [], count: 1, plan_id: p.plan_id,
      plan_state_changes: [{ trading_date: '2026-09-21', as_of_at: null, basis: 'daily', from: null, to: 'exit_signalled' }] } as unknown as EvaluationsResponse;
    expect(todayAction(p, evaluations, '2026-09-21').headline).toContain('清仓 5800 股');
  });
  it('a quality-rejected card is marked unusable and an expired one is hidden by status', () => {
    const rejected = { ...plan('600613.SH'), status: 'rejected_by_quality' as const, quality: [{ check_id: 'hard_stop_distance_sane', passed: false }] };
    expect(todayAction(rejected).short).toBe('不可用');
    expect(effectiveStatus(plan('600613.SH'), '2026-09-29')).toBe('expired');
    expect(effectiveStatus(plan('600613.SH'), '2026-09-21')).toBe('active');
    expect(validityRemaining(plan('600613.SH'), '2026-09-19')).toBe(5);
  });
});

describe('board summary', () => {
  it('sums the frozen current risk of the holding plans', () => {
    expect(totalRiskPct(plans)).toBeCloseTo(0.88 + 4.57 + 0.05 + 2.05, 2);
  });
  it('flags a snapshot older than the last close or followed by a fill', () => {
    const sessions = ['2026-09-18', '2026-09-21', '2026-09-22'];
    const snapshot = '2026-09-18T15:10:04+08:00';
    expect(lastClosedSession(sessions, new Date('2026-09-19T12:00:00+08:00'))).toBe('2026-09-18');
    expect(snapshotWarnings(snapshot, sessions, [], new Date('2026-09-19T12:00:00+08:00'))).toEqual([]);
    expect(snapshotWarnings(snapshot, sessions, [], new Date('2026-09-21T15:30:00+08:00'))).toEqual(['快照早于 09-21 收盘']);
    const fill = { record_id: 'x', trade_date: '2026-09-21', trade_time: '09:35:00', symbol: '600613.SH', side: 'sell', quantity: 4600, price: 8.5, verdicts: [] } as TradeFill;
    expect(snapshotWarnings(snapshot, sessions, [fill], new Date('2026-09-21T10:00:00+08:00'))).toEqual(['快照之后有 1 笔成交']);
  });
  it('names the nearest pending line and counts minute closes under the stop', () => {
    // take_partial is a minute line; the nearest daily pending line is the 8.69 confirm/no-add level
    expect(nearestPendingLine(plan('600613.SH'), 8.41)?.kind).toBe('no_add');
    expect(trailingBelow([7.7, 7.6, 7.62, 7.61], 7.63)).toBe(3);
    expect(trailingBelow([7.6, 7.7], 7.63)).toBe(0);
  });
});

describe('calibrated cap summary', () => {
  const calibrated = {
    ...plan('600613.SH').sizing!, target_exposure_pct: '25', cap_shares: 2900, max_shares: 1200, recommended_shares: 1200,
    binding_constraint: 'risk' as const,
    concentration_policy: 'tail_risk_advisory' as const,
    exposure_basis: { stage: 'crash_rebound', board: 'main_10', board_label: '主板（10%）', cell: 'crash_rebound|main_10',
      cap_pct: 25, q99_loss_pct: 18.9882, q95_loss_pct: 14.4553, samples: 64218, fallback: null, tolerance_pct: 5,
      percentile: 99, horizon_sessions: 2, calibration_version: 'discipline-exposure-calibration-v1:bc72676e05cc' },
  };
  it('names the cap, its data basis and the binding limit (600613 dry-run numbers)', () => {
    const summary = capSummary(calibrated)!;
    expect(summary.cap).toBe('集中度压力参考 25%（2900 股），仅提示');
    expect(summary.basis).toBe('急跌反弹 × 主板（10%）：两日最大跌幅 99% 分位 18.99%（64,218 个样本），压力参考 = 极端亏损 5% ÷ 18.99% 向下取 5 的倍数 = 25%');
    expect(summary.binding).toBe('风险上限 1200 股（1.0%÷止损距离）；集中度压力参考不触发减仓，建议上限 1200 股');
  });
  it('says when the cap binds and when a cell fell back', () => {
    const capBound = capSummary({ ...calibrated, binding_constraint: 'cap', recommended_shares: 500, cap_shares: 500,
      exposure_basis: { ...calibrated.exposure_basis, fallback: 'board_pooled_across_stages' } })!;
    expect(capBound.binding).toContain('集中度压力参考不触发减仓');
    expect(capBound.basis).toContain('样本不足按同板块合并');
  });
  it('labels a plan stored before the calibration as hand-picked', () => {
    expect(capSummary(plan('600613.SH').sizing)!.basis).toBe('无校准依据（旧版计划：手填阶段上限）');
  });
});

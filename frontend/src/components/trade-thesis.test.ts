import { describe, expect, it } from 'vitest';
import { benchmarkLabel, evidenceVersionNote, isEvidenceVersionChange, metricLabelText, observationValue, rankText, textValue, thesisChartAnnotations, thesisItems } from './trade-thesis';

describe('trade thesis presentation model', () => {
  it('projects the nested owner API contract without deriving a holding action', () => {
    const item = thesisItems({
      items: [{
        thesis_id: 'thesis-1', symbol: '600613.SH', revision: 2,
        thesis: { claim: '平台承接可能恢复', family: 'accumulation', source_run_id: 'run-1', available_at: '2026-09-17T07:00:00Z', origin_mode: 'prospective', original_rankings: [{ rank: 1, population: 34, lane: 'accumulation' }] },
        evaluation: {
          evaluation_id: 'eval-1', cutoff_at: '2026-09-17T07:00:00Z', content_hash: 'abc',
          states: { thesis_state: 'challenged', evidence_status: 'complete', entry_state: 'waiting' },
          changes_since_previous: [{ metric: 'flow', old_value: 1, new_value: -1 }],
          next_checks: [{ condition_id: 'platform', metric: 'close' }], current_rankings: [{ rank: 40, population: 94, lane: 'breakout', comparable_to_previous: false }],
        },
      }],
    })[0]!;
    expect(item).toMatchObject({ original_thesis: '平台承接可能恢复', thesis_state: 'challenged', entry_state: 'waiting' });
    expect(item.holding_action).toBeUndefined();
    expect(item.next_checks).toHaveLength(1);
    expect(item.original_rankings?.[0]?.rank).toBe(1);
    expect(item.current_rankings?.[0]?.comparable_to_previous).toBe(false);
  });

  it('uses numeric frozen original structure as a reference line, never as a hard stop', () => {
    const annotations = thesisChartAnnotations([{
      thesis_id: 't', revision: 1, symbol: '600613.SH',
      thesis: { original_structure: { support: 16, reference: 16.38, commentary: '忽略文本价位' } },
    }]);
    expect(annotations.map((item) => item.price)).toEqual([16, 16.38]);
    expect(annotations.every((item) => item.label.includes('参考') && !item.label.includes('止损'))).toBe(true);
    expect(annotations.every((item) => item.detail?.includes('不是持仓硬止损'))).toBe(true);
  });

  it('presents the actual evaluation vocabulary without leaking raw machine formatting', () => {
    expect(metricLabelText('amount_ratio_previous')).toBe('成交额 / 前一交易日');
    expect(benchmarkLabel('previous_5_sessions_mean')).toBe('前5个交易日均额');
    expect(observationValue(241232387, 'amount', 'CNY')).toBe('2.41 亿元');
    expect(observationValue(44.26, 'low', 'CNY')).toBe('44.26 元');
    expect(observationValue(0.8197726284508848, 'amount_ratio_previous', 'ratio')).toBe('0.82 倍');
    expect(rankText({ lane: 'accumulation', rank: 1, population: 34 })).toContain('潜伏观察');
    expect(textValue({ metric: 'close' })).toBe('价格与原结构的关系待验证');
    expect(textValue({ metric: 'full_entry_scenario_confirmed' })).toContain('不构成下单信号');
  });

  it('distinguishes an evidence version correction from a market move', () => {
    const change = { metric: 'amount', old_value: 241232.387, new_value: 241232387, unit: 'CNY', impact: 'evidence_version_changed', directly_comparable: false, old_version: 'legacy_thousand_cny', version: 'cny_v2' };
    expect(isEvidenceVersionChange(change)).toBe(true);
    expect(evidenceVersionNote(change)).toContain('数据口径/版本修正，非行情变化');
    expect(evidenceVersionNote(change)).toContain('legacy_thousand_cny → cny_v2');
  });

  it('marks changed ranking scopes as non-comparable and only charts explicit server lines', () => {
    expect(rankText({ rank: 40, population: 94, lane: '启动', comparable_to_previous: false })).toContain('启动 · 40/94');
    const annotations = thesisChartAnnotations([{
      thesis_id: 't', revision: 1, symbol: '600613.SH', chart_lines: [
        { label: '原平台', price: 16.38, kind: 'original_structure', valid_from: '2026-09-15' },
        { label: '无效价', price: Number.NaN, kind: 'current_plan' },
      ],
    }]);
    expect(annotations).toHaveLength(1);
    expect(annotations[0]).toMatchObject({ label: '原平台', price: 16.38, color: '#38bdf8' });
  });
});

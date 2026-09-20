import { describe, expect, it } from 'vitest';
import { rankText, thesisChartAnnotations, thesisItems } from './trade-thesis';

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

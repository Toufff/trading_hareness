import { describe, expect, it } from 'vitest';
import { observedHistory, signedPercent } from './sector-heat-presentation';

describe('sector heat presentation, without synthesizing data', () => {
  it('trims empty margins but preserves internal gaps and zero scores', () => {
    const points = [{ attention_score: null }, { attention_score: 0 }, { attention_score: null }, { attention_score: 80 }, { attention_score: null }];
    expect(observedHistory(points)).toEqual(points.slice(1, 4));
    expect(observedHistory(points, true)).toBe(points);
    expect(points).toHaveLength(5);
  });
  it('shows no invented history when all observations are missing', () => {
    expect(observedHistory([{ attention_score: null }])).toEqual([]);
  });
  it('does not turn missing returns into zero percent', () => {
    expect(signedPercent(null)).toBe('—');
    expect(signedPercent(0)).toBe('0.00%');
    expect(signedPercent(2.048)).toBe('+2.05%');
  });
});

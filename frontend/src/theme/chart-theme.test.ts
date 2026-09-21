import { describe, expect, it } from 'vitest';
// @ts-expect-error Node fs is provided by Vitest, not by the browser tsconfig.
import { readFileSync } from 'node:fs';
import { chartColors, guanshiChartTheme } from './chart-theme';

function luminance(hex: string) {
  const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return rgb[0]! * 0.2126 + rgb[1]! * 0.7152 + rgb[2]! * 0.0722;
}
describe('瑞鹤 theme contract', () => {
  it('keeps canvas colours aligned to CSS and text readable on paper', () => {
    const css: string = readFileSync('src/theme/tokens.css', 'utf8');
    for (const key of ['ink', 'muted', 'line', 'paper', 'sky', 'up', 'down'] as const) expect(css).toContain(chartColors[key]);
    for (const colour of [chartColors.ink, chartColors.muted, chartColors.sky, chartColors.up, chartColors.down]) {
      expect((luminance(chartColors.paper) + 0.05) / (luminance(colour) + 0.05)).toBeGreaterThan(4.5);
    }
    expect(guanshiChartTheme.candlestick.itemStyle.color).toBe(chartColors.up);
    expect(guanshiChartTheme.candlestick.itemStyle.color0).toBe(chartColors.down);
  });
});

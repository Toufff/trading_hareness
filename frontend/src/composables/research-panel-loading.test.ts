import { describe, expect, it, vi } from 'vitest';
import workspace from './useDashboardWorkspace.ts?raw';
import app from '../App.vue?raw';
import { panelsForResearchTab, RESEARCH_TAB_PANELS, settlePanelQueue } from './research-panel-loading';

describe('research tab request isolation', () => {
  it('maps every existing panel and tab; prioritizes current scan without laboratory fetches', () => {
    const source = workspace.slice(workspace.indexOf('function researchPanelEntries()'), workspace.indexOf('async function loadResearch()'));
    const allKeys = [...source.matchAll(/key: '([^']+)'/g)].map(match => match[1]);
    expect(new Set(Object.values(RESEARCH_TAB_PANELS).flat())).toEqual(new Set(allKeys));
    const selected = panelsForResearchTab(allKeys.map(key => ({key: key!})), 'close-review');
    expect(selected[0]?.key).toBe('post-close-strategy');
    expect(selected.some(item => item.key === 'strategy-experiments')).toBe(false);
    const panes = [...app.matchAll(/<el-tab-pane[^>]+name="([^"]+)"[^>]*>/g)];
    expect(panes).toHaveLength(11);
    for (const pane of panes) {
      expect(RESEARCH_TAB_PANELS[pane[1]!]).toBeDefined();
      expect(pane[0]).toContain(' lazy');
    }
  });

  it('bounds parallelism and keeps other panels usable after a failure', async () => {
    let active = 0; let peak = 0;
    const settled = vi.fn();
    await settlePanelQueue(Array.from({length: 15}, (_, i) => ({ key: String(i), run: async () => {
      peak = Math.max(peak, ++active);
      await new Promise(resolve => setTimeout(resolve, 1));
      active--;
      if (i === 0) throw new Error('one unavailable');
    }})), settled);
    expect(peak).toBe(4);
    expect(settled).toHaveBeenCalledTimes(15);
    expect(settled.mock.calls.filter(call => call[1] instanceof Error)).toHaveLength(1);
  });

  it('does not start queued work or mark cancelled data fresh after tab switch', async () => {
    const controller = new AbortController(); const settled = vi.fn(); const started: number[] = [];
    await settlePanelQueue(Array.from({length: 15}, (_, i) => ({key: String(i), run: async () => {
      started.push(i); controller.abort();
    }})), settled, controller.signal);
    expect(started).toEqual([0]);
    expect(settled).not.toHaveBeenCalled();
  });
});

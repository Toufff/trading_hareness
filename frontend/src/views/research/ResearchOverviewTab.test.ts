import { mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { describe, expect, it, vi } from 'vitest';

import ResearchOverviewTab from './ResearchOverviewTab.vue';
import { dashboardContextKey } from '../../dashboard-context';

function fakeDashboard() {
  return {
    actionLoading: '',
    postCloseRefresh: null,
    runPostCloseRefresh: vi.fn(),
    overview: { history_estimate: { estimated_storage_gib: 0, datasets: [] }, data_coverage: {}, feature_readiness: { decision_ready: true, blockers: [] } },
    historyDatasetRows: [],
    rowText: (value?: number) => String(value ?? '-'),
    storageText: (value?: number) => String(value ?? '-'),
    displayValue: (value: unknown) => (value === null || value === undefined ? '-' : String(value)),
    count: () => 0,
    readinessType: () => 'success',
    reconcileStaleFetchRuns: vi.fn(),
    runAction: vi.fn(),
    // The panel this test targets: a non-empty candidate pool alongside the badge.
    recommendations: [{ rank: 1, symbol: '000001.SZ', score: 0.8, decision: 'research_candidate' }],
    replayReadiness: { status: 'ready', p2_data_foundation_ready: true, p3_strategy_validation_ready: true, evidence: {} },
    mobileLayout: false,
    featureReadinessRows: [],
    featureStatusType: () => 'success',
    dateText: () => '-',
  };
}

describe('ResearchOverviewTab', () => {
  it('marks the latest candidate pool as research-only, not a trade instruction', () => {
    const wrapper = mount(ResearchOverviewTab, {
      global: {
        plugins: [ElementPlus],
        provide: { [dashboardContextKey as unknown as string]: fakeDashboard() },
      },
    });

    const notes = wrapper.findAll('[role="note"]');
    expect(notes.length).toBeGreaterThan(0);
    expect(notes.some((note) => note.text().includes('仅供研究参考'))).toBe(true);
  });
});

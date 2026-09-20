import { shallowMount } from '@vue/test-utils';
import { nextTick } from 'vue';
import ElementPlus from 'element-plus';
import { describe, expect, it, vi } from 'vitest';
import { useDashboardWorkspace } from '../../composables/useDashboardWorkspace';
import { dashboardContextKey } from '../../dashboard-context';
import ShortTermLanesPanel from '../../components/ShortTermLanesPanel.vue';
import CloseReviewTab from './CloseReviewTab.vue';

describe('CloseReviewTab asynchronous scan hydration', () => {
  it('renders a scan arriving after mount, and subsequent replacement, without remount', async () => {
    window.matchMedia ??= vi.fn().mockReturnValue({ matches: false });
    // Real shell proxyRefs contract, not a plain pre-populated snapshot.
    const dashboard = useDashboardWorkspace();
    const wrapper = shallowMount(CloseReviewTab, {
      global: { plugins: [ElementPlus], provide: { [dashboardContextKey as symbol]: dashboard } },
    });
    const panel = () => wrapper.findComponent(ShortTermLanesPanel);
    expect(panel().props('summary')).toBeUndefined();
    expect(panel().props('recommendation')).toBeNull();
    dashboard.formalRecommendation = { status: 'ready', decision_id: 'decision-1' };
    await nextTick();
    expect(panel().props('recommendation')).toMatchObject({ status: 'ready', decision_id: 'decision-1' });
    for (const day of ['2026-09-03', '2026-09-04']) {
      dashboard.postCloseStrategyRun = {
        status: 'completed', as_of_date: day,
        summary: { strategy_lanes: { as_of_date: day, status: 'completed', lanes: [] } },
      } as NonNullable<typeof dashboard.postCloseStrategyRun>;
      await nextTick();
      expect(panel().props('summary')).toMatchObject({ strategy_lanes: { as_of_date: day } });
    }
    wrapper.unmount();
  });
});

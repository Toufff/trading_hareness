import { mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { nextTick, proxyRefs, ref } from 'vue';
import { describe, expect, it, vi } from 'vitest';

import { dashboardContextKey } from '../../dashboard-context';
import StockStudyTab from './StockStudyTab.vue';

describe('StockStudyTab', () => {
  it('keeps launcher inputs writable through the proxyRefs dashboard context', async () => {
    const studySymbol = ref('000636.SZ');
    const studyLookback = ref(120);
    const runStockStudy = vi.fn();
    const dashboard = proxyRefs({
      studySymbol,
      studyLookback,
      stockStudy: ref(null),
      studyLoading: ref(false),
      studyError: ref(''),
      runStockStudy,
    }) as never;
    const wrapper = mount(StockStudyTab, {
      global: {
        plugins: [ElementPlus],
        provide: { [dashboardContextKey as symbol]: dashboard },
        stubs: { StockResearchWorkbench: true },
      },
    });

    await wrapper.get('input[placeholder="600487.SH"]').setValue('600487.SH');
    await nextTick();
    expect(studySymbol.value).toBe('600487.SH');

    const numberInput = wrapper.get('input[role="spinbutton"]');
    await numberInput.setValue('180');
    await numberInput.trigger('change');
    await nextTick();
    expect(studyLookback.value).toBe(180);

    await wrapper.get('button.el-button--primary').trigger('click');
    expect(runStockStudy).toHaveBeenCalledOnce();
  });
});

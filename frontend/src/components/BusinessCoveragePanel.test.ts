import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import BusinessCoveragePanel from './BusinessCoveragePanel.vue';
afterEach(() => vi.unstubAllGlobals());
it('loads on demand and never labels partial coverage healthy', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({status:'partial',checked_at:'2026-09-22',checks:[
    {key:'discipline',label:'纪律提醒覆盖',status:'attention',reason:'不能绕过质量检查',evidence:{coverage:{holdings:{expected:5,covered:2,missing_symbols:['600613.SH']}}}},
  ]})));
  vi.stubGlobal('fetch',fetch);
  const wrapper=mount(BusinessCoveragePanel);
  expect(fetch).not.toHaveBeenCalled();
  await wrapper.get('button').trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('持仓：2/5');
  expect(wrapper.text()).toContain('不能认定整套系统已可用');
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/research/strategy/business-coverage');
  wrapper.unmount();
});

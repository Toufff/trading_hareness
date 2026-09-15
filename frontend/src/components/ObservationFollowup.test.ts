import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import ObservationFollowup, { type FollowupItem } from './ObservationFollowup.vue';

it('keeps both winners and losers after they disappear from the new scan', async () => {
  const wrapper = mount(ObservationFollowup);
  expect(wrapper.text()).toContain('尚无跟踪账本');
  const item = (name: string, n: number): FollowupItem => ({ origin_id:name, name, symbol:'600001.SH',signal_date:'2026-09-10',lane:'accumulation',display_rank:1,rank:1,status:'tracking',timing:'reconstructed',expected_sessions:1,latest_return_pct:n,path_check:'both_touched_order_unknown',original_confirmation:'先确认',original_invalidation:'原失效',windows:{'1':{status:'observed',return_pct:n},'3':{status:'not_due',return_pct:null}} });
  await wrapper.setProps({followup:{status:'completed',note:'不是交易收益',total:2,items:[item('嘉立创',10),item('日出东方',-10)]}});
  expect(wrapper.text()).toContain('嘉立创'); expect(wrapper.text()).toContain('日出东方');
  expect(wrapper.text()).toContain('+10.00%'); expect(wrapper.text()).toContain('-10.00%');
  expect(wrapper.text()).toContain('未到期'); expect(wrapper.text()).toContain('历史补录');
  expect(wrapper.text()).toContain('先后未知');
  await wrapper.get('input[aria-label="搜索往期股票"]').setValue('日出');
  expect(wrapper.text()).not.toContain('嘉立创');expect(wrapper.text()).toContain('日出东方');
  wrapper.unmount();
});

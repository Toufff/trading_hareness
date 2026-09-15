import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import PriceVolumeEvidence from './PriceVolumeEvidence.vue';

it('separates calculated facts, warnings, references and unverified triggers after late hydration', async () => {
  const wrapper = mount(PriceVolumeEvidence);
  expect(wrapper.text()).toContain('未提供不代表检查通过');
  await wrapper.setProps({evidence:{version:'pv-test',status:'quality_warning',scope:'真实日线',
    reasons:['今日成交额较大'],warnings:['长上影，收盘成果有限'],
    metrics:{amount_multiple:1.4,volume_multiple:null,close_location:0.3,advance_mean_amount:5e8},
    references:{prior10_high:12.3},unverified:['limit_board_process','execution']}});
  expect(wrapper.text()).toContain('收盘在全天区间的位置');
  expect(wrapper.text()).toContain('30.0%'); expect(wrapper.text()).toContain('5.00 亿元');
  expect(wrapper.text()).toContain('成交股数相对均值倍数缺少数据');
  expect(wrapper.text()).toContain('尚未验证：封板、开板和回封过程');
  expect(wrapper.text()).toContain('前十日最高价12.30 元');
  expect(wrapper.text()).toContain('不是已经发生的事实');
  expect(wrapper.text()).not.toContain('limit_board_process');
  wrapper.unmount();
});

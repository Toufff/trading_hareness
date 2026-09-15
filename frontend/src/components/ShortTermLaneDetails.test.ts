import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import ShortTermLaneDetails from './ShortTermLaneDetails.vue';

it('keeps restricted leaders visible when conditional shortlist is empty', () => {
  const wrapper = mount(ShortTermLaneDetails, { props: { lane: {
    key: 'relay', label: '接力', purpose: '观察', total_matches: 1,
    selected: [], caution_list: [], empty_reason: '风险受限', observation_policy: '观察不是可买',
    observation_list: [{ symbol: '000636.SZ', name: '风华高科', sector_label: '元件',
      reason: '涨停同伴', confirmation: '等待承接', invalidation: '结构破坏',
      caution: '不证明可成交', expiry: '下一交易日',
      metrics: {close: 55.99, change_pct: 10, amount: 9984863608, turnover: 16.22},
      liquidity: {score:100}, attention: {participation_percentile:95, sample_count:10, amount_multiple:2.5} }],
  } } });
  expect(wrapper.get('[data-testid="independent-discovery"]').text()).toContain('风华高科');
  expect(wrapper.text()).toContain('不证明可成交');
  expect(wrapper.text()).toContain('95 分位');
  wrapper.unmount();
});

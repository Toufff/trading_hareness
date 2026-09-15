import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import ReviewSelectionPanel from './ReviewSelectionPanel.vue';

describe('review selection provenance', () => {
  it('renders reasons and segregates background after late data arrival', async () => {
    const wrapper = mount(ReviewSelectionPanel, { props: { scan: {} } });
    const item = { symbol: '601929.SH', name: '吉视传媒', business: '广电', risk: '亏损', conclusion: '保留资金观察',
      selection_reason: '潜伏观察展示第1：合计流入且区间收窄', outcome_label: '保留观察',
      selection: { question: '资金改善是否意味着利润反转？', why_now: '代表复核' }, sources: [] };
    await wrapper.setProps({ scan: { review_policy: '按策略选代表，不按查过与否排序',
      review_coverage: { planned: 1, completed: 1, missing_symbols: [] },
      review_groups: [{ key: 'priority', label: '本轮优先复核', items: [item] },
        { key: 'background', label: '背景资料', items: [{ ...item, symbol: '603160.SH', name: '汇顶科技' }] }] } });
    expect(wrapper.find('[data-review-group="priority"]').text()).toContain('为什么复核：潜伏观察展示第1');
    expect(wrapper.text()).toContain('资金改善是否意味着利润反转');
    expect(wrapper.find('[data-review-group="background"]').element.tagName).toBe('DETAILS');
    expect(wrapper.find('[data-review-group="priority"]').text()).not.toContain('汇顶科技');
  });
  it('does not silently replace a missing representative', () => {
    const wrapper = mount(ReviewSelectionPanel, { props: { scan: { review_groups: [],
      review_coverage: { planned: 1, completed: 0, missing_symbols: ['600001.SH'] },
      review_plan: [{ symbol: '600001.SH', name: '代表甲', selection_reason: '策略首位' }] } } });
    expect(wrapper.text()).toContain('代表甲（600001）');
    expect(wrapper.text()).toContain('不能用其他股票冒充完成');
  });
});

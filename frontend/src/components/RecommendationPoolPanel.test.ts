import { mount } from '@vue/test-utils';
import { describe, it, expect } from 'vitest';
import Panel from './RecommendationPoolPanel.vue';
describe('formal recommendation panel', () => {
  it('does not relabel missing research as no opportunities', () => {
    expect(mount(Panel).text()).toContain('尚未发布');
  });
  it('renders persisted decision, reasons and stale warning', async () => {
    const value = {status:'ready', decision_id:'frozen-id', as_of_date:'2026-09-14', recommended:[{symbol:'603936.SH',name:'博敏电子',priority:1,stage:'strong_pullback',why_now:'趋势',comparison:'比红板强',trigger:'回踩确认',invalidation:'结构失效'}]};
    const w = mount(Panel, {props:{value}});
    expect(w.attributes('data-decision-id')).toBe('frozen-id');
    expect(w.text()).toContain('比红板强');
    expect(w.text()).toContain('回踩确认');
    await w.setProps({value:{...value,status:'stale',notice:'历史推荐不能同步'}});
    expect(w.find('[role="status"]').text()).toContain('历史推荐不能同步');
  });
});

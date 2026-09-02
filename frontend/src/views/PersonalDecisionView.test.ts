import { flushPromises, mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { afterEach, describe, expect, it, vi } from 'vitest';

import PersonalDecisionView from './PersonalDecisionView.vue';

const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { 'content-type': 'application/json' },
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('PersonalDecisionView', () => {
  it('marks a conditional-buy new-buy plan as research-only, not a trade instruction', async () => {
    const brief = {
      status: 'ready',
      as_of_at: '2026-09-01T15:15:00+08:00',
      market: { status: 'ready', content: {} },
      holdings: { status: 'ready', actions: [] },
      new_buys: {
        status: 'ready',
        actions: [{
          plan_key: 'buy-1', plan_kind: 'new_buy', symbol: '000001.SZ', name: '示例股票',
          action: 'buy_on_trigger', exit_trigger: '跌破止损', max_position_pct: 5, valid_until: '2026-09-05',
          rationale: [],
        }],
      },
      delivery: { market_eligible: true, holding_actions_eligible: true, new_buy_actions_eligible: true },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(brief))
      .mockResolvedValueOnce(jsonResponse({ as_of_date: '2026-09-01', summary: { total: 0, passed: 0, rejected: 0, incomplete: 0 }, items: [] }));
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(PersonalDecisionView, { global: { plugins: [ElementPlus] } });
    await flushPromises();

    const buyCard = wrapper.find('.buy-card');
    expect(buyCard.exists()).toBe(true);
    const note = buyCard.find('[role="note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain('仅供研究参考');
  });
});

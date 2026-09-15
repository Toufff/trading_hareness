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
    const buyPlan = {
          plan_key: 'buy-1', plan_kind: 'new_buy', symbol: '000001.SZ', name: '示例股票',
          action: 'buy_on_trigger', exit_trigger: '跌破止损', max_position_pct: 5, valid_until: '2026-09-05',
          rationale: [],
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-01T15:15:00+08:00', content: {}, delivery: { eligible: true, complete: true } }))
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-01T15:15:00+08:00', actions: [buyPlan], delivery: { eligible: true } }))
      .mockResolvedValueOnce(jsonResponse({ as_of_date: '2026-09-01', summary: { total: 0, passed: 0, rejected: 0, incomplete: 0 }, items: [] }))
      .mockResolvedValueOnce(jsonResponse({ as_of_date: '2026-09-01', status: 'completed', research_only: true, depends_on_holdings: false, total_unique: 1, items: [{ symbol: '600001.SH', name: '扫描股票', lane_keys: ['trend'], lane_labels: ['主线趋势'], reason: '全市场命中', confirmation: '放量突破', invalidation: '跌破平台', review_status: 'technical_observation', review_label: '量价观察，尚未升级', buy_authorized: false, depends_on_holdings: false }] }));
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(PersonalDecisionView, { props: { mode: 'market' }, global: { plugins: [ElementPlus] } });
    await flushPromises();

    const buyCard = wrapper.find('.buy-card');
    expect(buyCard.exists()).toBe(true);
    expect(buyCard.find('.chart-launch').text()).toBe('查看图形');
    expect(wrapper.find('.chart-entry-card').text()).toContain('按代码打开图形');
    expect(wrapper.find('.chart-entry-card').text()).not.toContain('示例股票');
    expect(wrapper.find('.market-scan-section').text()).toContain('扫描股票');
    expect(wrapper.find('.market-scan-section').text()).not.toContain('示例股票');
    const note = buyCard.find('[role="note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain('仅供研究参考');
    expect(wrapper.text()).not.toContain('账户持仓建议');
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('holding-advice'))).toBe(false);
  });

  it('renders holdings as a separate surface without loading market or scan endpoints', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      status: 'ready', as_of_at: '2026-09-04T15:15:00+08:00', portfolio_observed_at: '2026-09-04T15:15:00+08:00',
      freshness_status: 'current', actions: [], delivery: { eligible: true },
    }));
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(PersonalDecisionView, { props: { mode: 'holdings' }, global: { plugins: [ElementPlus] } });
    await flushPromises();

    expect(wrapper.text()).toContain('我的持仓');
    expect(wrapper.text()).toContain('账户持仓建议');
    expect(wrapper.text()).not.toContain('全市场扫描观察');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toContain('/api/research/personal/holding-advice/latest');
  });

  it('explains that stale holdings require a user-initiated sync and shows observed_at', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      status: 'ready', as_of_at: '2026-09-04T15:15:00+08:00', portfolio_observed_at: '2026-09-04T15:15:00+08:00',
      reference_trade_date: '2026-09-12', freshness_status: 'stale', actions: [], delivery: { eligible: false },
    }));
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(PersonalDecisionView, { props: { mode: 'holdings' }, global: { plugins: [ElementPlus] } });
    await flushPromises();

    expect(wrapper.text()).toContain('持仓按用户主动同步');
    expect(wrapper.text()).toContain('请登录电脑交易账户并主动同步');
    expect(wrapper.text()).toContain('持仓实际读取时间：2026-09-04T15:15:00+08:00');
    expect(wrapper.text()).not.toContain('等待12:00/15:15');
  });

  it('distinguishes an unread holdings snapshot from an expired snapshot', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      status: 'blocked', as_of_at: '2026-09-13T12:00:00+08:00', freshness_status: 'stale_or_unverified', actions: [], delivery: { eligible: false },
    }));
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(PersonalDecisionView, { props: { mode: 'holdings' }, global: { plugins: [ElementPlus] } });
    await flushPromises();

    expect(wrapper.text()).toContain('尚未读取到持仓');
    expect(wrapper.text()).toContain('请登录电脑交易账户并主动同步');
    expect(wrapper.text()).not.toContain('持仓已过期');
    expect(wrapper.text()).not.toContain('持仓实际读取时间');
  });

  it('renders a durable user tracking tag alongside later strategy tags', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-05T15:15:00+08:00', content: {}, delivery: { eligible: true, complete: true } }))
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-05T15:15:00+08:00', actions: [], delivery: { eligible: false } }))
      .mockResolvedValueOnce(jsonResponse({ as_of_date: '2026-09-05', summary: { total: 0, passed: 0, rejected: 0, incomplete: 0 }, items: [] }))
      .mockResolvedValueOnce(jsonResponse({
        as_of_date: '2026-09-05', status: 'completed', research_only: true, depends_on_holdings: false,
        total_unique: 1, strategy_total_unique: 1, user_tracking_total: 1,
        items: [{
          symbol: '600664.SH', name: '哈药股份', lane_keys: ['accumulation'], lane_labels: ['潜伏观察'],
          tags: [
            { key: 'user_requested_tracking', label: '用户主动跟踪', source: 'user' },
            { key: 'strategy:accumulation', label: '潜伏观察', source: 'strategy' },
          ],
          user_requested_tracking: true, review_status: 'technical_observation', review_label: '量价观察，尚未升级',
          tracking_research: {
            version: 'user-tracking-research-v1', status: 'complete', stance: 'risk_repair',
            headline: '价格与资金仍处于风险修复阶段。', as_of_date: '2026-09-04',
            price_structure: { close: 7.78 }, liquidity: { turnover_rate: 17.83 },
            valuation: { pe: 38.26, pb: 3.21 },
            capital_flow: { windows: { '5': { net_amount: -509300000 } } },
            sector: { label: '化学制药' }, events: [{ title: '2026年半年度报告' }],
            conditions: { confirmation: '站回20日均线', range: '缩量横盘', invalidation: '跌破支撑' },
            risks: [], unavailable_sections: [], buy_authorized: false, depends_on_holdings: false,
          },
          buy_authorized: false, depends_on_holdings: false,
        }],
      }));
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(PersonalDecisionView, { props: { mode: 'market' }, global: { plugins: [ElementPlus] } });
    await flushPromises();

    const card = wrapper.find('.market-scan-section');
    expect(card.text()).toContain('哈药股份');
    expect(card.text()).toContain('用户主动跟踪');
    expect(card.text()).toContain('潜伏观察');
    expect(card.text()).toContain('主动跟踪分析');
    expect(card.text()).toContain('价格与资金仍处于风险修复阶段');
    expect(card.text()).toContain('5日资金');
    expect(card.text()).toContain('2026年半年度报告');
    expect(card.text()).toContain('站回20日均线');
  });
});

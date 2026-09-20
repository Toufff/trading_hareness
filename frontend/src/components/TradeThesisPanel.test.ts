import { flushPromises, mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { afterEach, describe, expect, it, vi } from 'vitest';
import TradeThesisPanel from './TradeThesisPanel.vue';

afterEach(() => { vi.restoreAllMocks(); });

describe('TradeThesisPanel', () => {
  it('shows conclusion-first nested data and the historical cutoff', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [{
      thesis_id: 't-1', symbol: '600613.SH', revision: 1,
      thesis: { claim: '资金横盘等待启动', origin_mode: 'prospective' },
      evaluation: { states: { thesis_state: 'challenged', evidence_status: 'complete', entry_state: 'waiting' }, cutoff_at: '2026-09-17T07:00:00Z', next_checks: [{ metric: '平台承接' }] },
    }] }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const wrapper = mount(TradeThesisPanel, { props: { symbol: '600613.SH', asOf: '2026-09-17' }, global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(wrapper.text()).toContain('资金横盘等待启动');
    expect(wrapper.text()).toContain('当前新买');
    expect(wrapper.text()).toContain('未加载有效持仓计划');
    expect(wrapper.text()).toContain('历史视图截至 2026-09-17');
    wrapper.unmount();
  });

  it('renders transport failures explicitly instead of an empty state', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502, headers: { 'content-type': 'text/html' } }));
    const wrapper = mount(TradeThesisPanel, { props: { symbol: '600613.SH' }, global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(wrapper.text()).toContain('交易假设加载失败');
    expect(wrapper.text()).toContain('非 JSON 响应');
    wrapper.unmount();
  });

  it('shows first-evaluation observations and keeps market date separate from knowledge cutoff', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [{
      thesis_id: 't-first', symbol: '600613.SH', revision: 1,
      thesis: { claim: '首次冻结假设' },
      evaluation: {
        states: { thesis_state: 'challenged', evidence_status: 'complete', entry_state: 'waiting' },
        changes_since_previous: [], observations: [
          { evidence_id: 'e1', metric: 'amount_vs_previous', value: 0.81, unit: 'x', benchmark: 'previous_day' },
          { evidence_id: 'e2', metric: 'amount_vs_5d_mean', value: 1.06, unit: 'x', benchmark: 'five_day_mean' },
        ],
      },
    }] }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const wrapper = mount(TradeThesisPanel, { props: { symbol: '600613.SH', marketAsOf: '2026-09-18' }, global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(fetchSpy.mock.calls[0]?.[0]).toBe('/api/research/theses?symbol=600613.SH');
    expect(wrapper.text()).toContain('行情数据截至 2026-09-18');
    expect(wrapper.text()).toContain('首次评价，无上轮差异');
    expect(wrapper.text()).toContain('成交额 / 前一日');
    expect(wrapper.text()).toContain('0.81 倍');
    expect(wrapper.text()).toContain('基准 前5日均额');
    expect(wrapper.text()).toContain('受挑战');
    expect(wrapper.text()).toContain('等待确认');
    wrapper.unmount();
  });

  it('distinguishes an unchanged repeat from a first evaluation', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [{
      thesis_id: 't-repeat', symbol: '600613.SH', revision: 1, thesis: { claim: '持续观察' },
      evaluation: { previous_evaluation_id: 'eval-before', changes_since_previous: [],
        states: { thesis_state: 'supported', evidence_status: 'complete', entry_state: 'waiting' } },
    }] }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const wrapper = mount(TradeThesisPanel, { props: { symbol: '600613.SH' }, global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(wrapper.text()).toContain('较上轮无新增证据变化');
    expect(wrapper.text()).not.toContain('首次评价，无上轮差异');
    wrapper.unmount();
  });

  it('does not present a data version correction as a market jump', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [{
      thesis_id: 't-version', symbol: '600613.SH', revision: 1, thesis: { claim: '数据修正测试' },
      evaluation: { states: { thesis_state: 'supported', evidence_status: 'complete', entry_state: 'waiting' }, changes_since_previous: [{
        metric: 'amount', old_value: 241232.387, new_value: 241232387, unit: 'CNY',
        impact: 'evidence_version_changed', directly_comparable: false, old_version: 'legacy_thousand_cny', version: 'cny_v2',
      }] },
    }] }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const wrapper = mount(TradeThesisPanel, { props: { symbol: '600613.SH' }, global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(wrapper.text()).toContain('当前 2.41 亿元');
    expect(wrapper.text()).toContain('数据口径/版本修正，非行情变化');
    expect(wrapper.text()).toContain('旧口径值见审计时间轴');
    expect(wrapper.text()).not.toContain('2412.32 万元 →');
    wrapper.unmount();
  });
});

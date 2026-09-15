import { flushPromises, mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { describe, expect, it, vi } from 'vitest';
vi.mock('vue-echarts', () => ({ default: { template: '<div data-test="chart" />' } }));
import SectorHeatPanel from './SectorHeatPanel.vue';

const payload = {
  sector_heat: {
    schema_version: 1,
    model_version: 'sector-heat-v1',
    generated_at: '2026-09-09T08:00:00Z',
    trade_date: '2026-09-09',
    phase: 'close',
    status: 'ready',
    source: 'longhu',
    warnings: ['部分板块缺少广度'],
    items: [
      { key: 'industry:bank', name: '银行', kind: 'industry', code: 'BK001', coverage: 0.9, attention_score: 88, activity_score: 71, strength_score: 80, risk_score: 32, rank: 1, reasons: ['成交参与提升'], metrics: { amount: 100, main_net: 12, return_pct: 1.2 }, daily_history: [{ trade_date: '2026-09-09', attention_score: 88 }, { trade_date: '2026-09-08', attention_score: null }] },
      { key: 'concept:robot', name: '机器人', kind: 'concept', coverage: 0.8, attention_score: 77, activity_score: 90, strength_score: 84, risk_score: 66, rank: 1 },
    ],
  },
};

describe('SectorHeatPanel', () => {
  it('keeps the category count independent of search and collapses audit material', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }))));
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus], stubs: { VChart: true } } });
    await flushPromises(); await flushPromises();
    expect(wrapper.find('.evidence-section').attributes('open')).toBeUndefined();
    await wrapper.get('input[type="search"]').setValue('no-match');
    expect(wrapper.find('.rank-sort').text()).toContain('0 / 1 个');
    expect(wrapper.text()).toContain('没有匹配的板块');
    wrapper.unmount();
  });
  it('distinguishes pending and timed-out requests from missing data', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockImplementation((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    })));
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus], stubs: { VChart: true } } });
    try {
      await wrapper.vm.$nextTick();
      expect(wrapper.text()).toContain('正在读取板块排行榜');
      expect(wrapper.text()).not.toContain('尚未发布');
      await vi.advanceTimersByTimeAsync(15001);
      await flushPromises();
      expect(wrapper.text()).toContain('请求超过15秒');
      expect(wrapper.text()).not.toContain('尚未发布');
    } finally { wrapper.unmount(); vi.useRealTimers(); }
  });
  it('renders fetched detail reactively, switches categories, and refreshes the same selected key', async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.includes('?key=')
        ? { sector_heat: { items: payload.sector_heat.items.filter((item) => url.endsWith(encodeURIComponent(item.key))).map((item) => ({ ...item, reasons: [`详情-${item.name}`] })) } }
        : { sector_heat: { ...payload.sector_heat, items: payload.sector_heat.items.map(({ reasons, daily_history, ...item }) => item) } };
      return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus] } });
    await flushPromises(); await flushPromises();
    expect(wrapper.text()).toContain('详情-银行');
    Object.assign(wrapper.vm, { selectedKind: 'concept' });
    await flushPromises(); await flushPromises();
    expect(wrapper.text()).toContain('详情-机器人');
    const before = fetchMock.mock.calls.filter(([url]) => String(url).includes('?key=')).length;
    await wrapper.findAll('button').find((button) => button.text() === '刷新')!.trigger('click');
    await flushPromises(); await flushPromises();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('?key=')).length).toBeGreaterThan(before);
    expect(wrapper.text()).toContain('详情-机器人');
    wrapper.unmount();
  });
  it('loads the read-only snapshot and filters industry/concept rows', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus], stubs: { VChart: true } } });
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/sector-heat', expect.objectContaining({ signal: expect.any(AbortSignal) }));
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/sector-heat?key=industry%3Abank', expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(wrapper.text()).toContain('银行');
    expect(wrapper.text()).toContain('部分板块缺少广度');
    Object.assign(wrapper.vm, { selectedKind: 'concept' });
    await flushPromises();
    await flushPromises();
    expect(wrapper.text()).toContain('机器人');
    expect(wrapper.text()).not.toContain('银行');
  });

  it('shows an explicit unpublished state for a missing payload', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })));
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus], stubs: { VChart: true } } });
    await flushPromises();
    expect(wrapper.text()).toContain('板块热度尚未发布');
  });

  it('supports search and cancels an in-flight request on unmount', async () => {
    let resolve!: (value: Response) => void;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((done) => { resolve = done; }));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus], stubs: { VChart: true } } });
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    wrapper.unmount();
    expect((request.signal as AbortSignal).aborted).toBe(true);
    resolve(new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } }));
  });

  it('keeps list visible when detail request fails', async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => String(input).includes('?key=') ? Promise.reject(new Error('detail unavailable')) : Promise.resolve(new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } })));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(SectorHeatPanel, { global: { plugins: [ElementPlus], stubs: { VChart: true } } });
    await flushPromises(); await flushPromises();
    expect(wrapper.text()).toContain('银行');
    expect(wrapper.text()).toContain('板块详情读取失败');
  });
});

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ElementPlus from 'element-plus';
import plansFixture from '../../../e2e/fixtures/discipline/plans-latest.json';
import reconciliations from '../../../e2e/fixtures/discipline/reconciliations.json';
import DisciplineBoard from './DisciplineBoard.vue';
import DisciplinePoolCard from './DisciplinePoolCard.vue';

const fixtures = import.meta.glob('../../../e2e/fixtures/discipline/*.json', { eager: true, import: 'default' }) as Record<string, unknown>;
const fixture = (name: string) => fixtures[`../../../e2e/fixtures/discipline/${name}`];
const symbolOf = (planId: string) => (plansFixture.items as Array<{ plan_id: string; symbol: string }>).find((item) => item.plan_id === planId)?.symbol;

function respond(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }));
}

function fakeFetch(overrides: Record<string, () => Promise<Response>> = {}) {
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => {
    const url = String(input);
    calls.push(`${init?.method ?? 'GET'} ${url}`);
    for (const [prefix, handler] of Object.entries(overrides)) if (url.startsWith(prefix)) return handler();
    if (url.startsWith('/api/research/discipline/plans/latest')) return respond(plansFixture);
    if (url.startsWith('/api/research/discipline/reconciliations')) return respond(reconciliations);
    const plan = /\/plans\/([0-9a-f-]{36})\/(chart|evaluations)/.exec(url);
    if (plan) return respond(fixture(`${plan[2] === 'chart' ? 'chart' : 'evaluations'}-${symbolOf(plan[1]!)}.json`));
    const history = /plans\/history\?.*symbol=([0-9A-Z.]+)/.exec(decodeURIComponent(url));
    if (history) return respond(fixture(`history-${history[1]}.json`));
    return respond({ detail: 'unexpected' }, 404);
  }));
  return calls;
}

const stubs = { DisciplineChart: { template: '<div data-testid="chart-stub" />' } };
const NOW = new Date('2026-09-19T12:00:00+08:00');

afterEach(() => { vi.unstubAllGlobals(); });

describe('DisciplineBoard', () => {
  it('shows today\'s action summary first, then the list and the selected card; reads only', async () => {
    const calls = fakeFetch();
    const wrapper = mount(DisciplineBoard, { props: { accountKey: 'citics-primary', now: NOW, snapshotObservedAt: '2026-09-18T15:10:04+08:00' },
      global: { plugins: [ElementPlus], stubs } });
    await flushPromises(); await flushPromises();
    const summary = wrapper.get('[data-testid=discipline-summary]');
    expect(summary.text()).toContain('今日动作汇总');
    expect(summary.text()).toContain('当前总风险 7.55%');
    const rows = summary.findAll('.summary-row');
    expect(rows.map((row) => row.attributes('data-symbol'))).toEqual(['000977.SZ', '600613.SH', '600664.SH', '603823.SH']);
    expect(rows[1]!.text()).toContain('09-21 开盘15分钟内减到 1200 股（卖 4600，可卖 5800）');
    expect(rows[1]!.text()).toContain('硬止损 7.63 · 9.3% / 0.9×ATR');
    expect(rows[0]!.text()).toContain('清仓 300 股');
    // summary precedes the card list in reading order
    const html = wrapper.html();
    expect(html.indexOf('discipline-summary')).toBeLessThan(html.indexOf('纪律卡列表'));
    expect(wrapper.find('[data-testid=snapshot-warning]').exists()).toBe(false);
    // new-buy plans stay off the holdings board
    expect(wrapper.text()).not.toContain('冰轮环境');
    await rows[1]!.trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-testid=discipline-detail]').attributes('data-plan-id')).toBe('699144a6-00e4-42a2-bdf7-168e601bfef0');
    expect(wrapper.get('[data-testid=today-action]').text()).toContain('减到 1200 股');
    expect(wrapper.get('[data-testid=discipline-line-table]').text()).toContain('禁加仓');
    expect(calls.every((call) => call.startsWith('GET '))).toBe(true);
    expect(calls.every((call) => call.includes('/api/research/discipline/'))).toBe(true);
  });

  it('warns that holdings may have changed when a fill is later than the snapshot', async () => {
    fakeFetch({ '/api/research/discipline/reconciliations': () => respond({ ...reconciliations, trades: [
      { record_id: 't', trade_date: '2026-09-18', trade_time: '14:50:00', symbol: '600613.SH', side: 'sell', quantity: 100, price: 8.4, verdicts: [] },
    ] }) });
    const wrapper = mount(DisciplineBoard, { props: { accountKey: 'citics-primary', now: NOW, snapshotObservedAt: '2026-09-18T14:00:00+08:00' },
      global: { plugins: [ElementPlus], stubs } });
    await flushPromises(); await flushPromises();
    expect(wrapper.get('[data-testid=snapshot-warning]').text()).toContain('持仓可能已变');
  });

  it('explains how to get a card when none exists, and hides expired cards by default', async () => {
    fakeFetch({ '/api/research/discipline/plans/latest': () => respond({ ...plansFixture, items: [], count: 0 }) });
    const empty = mount(DisciplineBoard, { props: { accountKey: 'citics-primary', now: NOW }, global: { plugins: [ElementPlus], stubs } });
    await flushPromises();
    expect(empty.get('[data-testid=discipline-empty]').text()).toContain('尚未生成纪律卡，运行 stock-discipline 生成');

    fakeFetch();
    const later = mount(DisciplineBoard, { props: { accountKey: 'citics-primary', now: new Date('2026-10-08T10:00:00+08:00') },
      global: { plugins: [ElementPlus], stubs } });
    await flushPromises(); await flushPromises();
    expect(later.get('[data-testid=discipline-empty]').text()).toContain('均已过期');
  });

  it('shows the failure reason and retries', async () => {
    fakeFetch({ '/api/research/discipline/plans/latest': () => respond({ detail: '数据库暂不可用' }, 503) });
    const wrapper = mount(DisciplineBoard, { props: { accountKey: 'citics-primary', now: NOW }, global: { plugins: [ElementPlus], stubs } });
    await flushPromises();
    expect(wrapper.get('[data-testid=discipline-error]').text()).toContain('数据库暂不可用');
    fakeFetch();
    await wrapper.get('[data-testid=discipline-error] button').trigger('click');
    await flushPromises(); await flushPromises();
    expect(wrapper.find('[data-testid=discipline-summary]').exists()).toBe(true);
  });

  it('marks a quality-rejected card as unusable with a red border', async () => {
    const rejected = { ...plansFixture, items: plansFixture.items.map((item) => item.symbol === '600664.SH'
      ? { ...item, status: 'rejected_by_quality', quality: [{ check_id: 'hard_stop_distance_sane', passed: false, detail: '距离 13.9%' }] } : item) };
    fakeFetch({ '/api/research/discipline/plans/latest': () => respond(rejected) });
    const wrapper = mount(DisciplineBoard, { props: { accountKey: 'citics-primary', now: NOW }, global: { plugins: [ElementPlus], stubs } });
    await flushPromises(); await flushPromises();
    const item = wrapper.get('.list-item[data-symbol="600664.SH"]');
    expect(item.classes()).toContain('rejected');
    expect(item.text()).toContain('不可用');
  });
});

describe('DisciplinePoolCard', () => {
  it('expands the new-buy card of a pool pick with its research conditions', async () => {
    fakeFetch();
    const wrapper = mount(DisciplinePoolCard, { props: { symbol: '000811.SZ', name: '冰轮环境', accountKey: 'citics-primary' },
      global: { plugins: [ElementPlus], stubs }, attachTo: document.body });
    await wrapper.get('button.toggle').trigger('click');
    await flushPromises(); await flushPromises();
    const panel = document.querySelector('[data-testid=pool-discipline-panel]')!;
    expect(panel).not.toBeNull();
    expect(panel.textContent).toContain('新买计划');
    expect(panel.textContent).toContain('等待触发：收盘站上 37.94、不高于 42.74 时最多买 300 股');
    expect(panel.textContent).toContain('追高上限');
    wrapper.unmount();
  });

  it('says how to generate a card when the pick has none', async () => {
    fakeFetch();
    const wrapper = mount(DisciplinePoolCard, { props: { symbol: '600000.SH', name: '浦发银行', accountKey: 'test-empty' },
      global: { plugins: [ElementPlus], stubs } });
    await wrapper.get('button.toggle').trigger('click');
    await flushPromises();
    expect(document.body.textContent).toContain('尚未生成新买纪律卡，运行 stock-discipline 生成');
    wrapper.unmount();
  });
});

import { mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ShortTermLanesPanel from './ShortTermLanesPanel.vue';
import { downloadStrategyReport, type StrategyScan } from './short-term-reports';
import * as http from '../api/http';

function sample(): StrategyScan {
  const makePick = (name: string, symbol: string) => ({ name, symbol, reason: `${name}入选原因`,
    confirmation: `${name}确认`, invalidation: `${name}放弃`, caution: '勿追高', expiry: '下一交易日',
    sector_label: '测试行业', metrics: { close: 10, change_pct: 2, amount: 500000000, turnover: 4 } });
  const lanes = [
    { key: 'expansion', label: '放量启动', purpose: '找启动', total_matches: 8, selected: [makePick('股票甲', '600001.SH')], empty_reason: '没有匹配' },
    { key: 'pullback', label: '强势回踩', purpose: '找回踩', total_matches: 3, selected: [makePick('股票乙', '600002.SH')], empty_reason: '没有匹配' },
    { key: 'event', label: '事件机会', purpose: '找事件', total_matches: 0, selected: [], empty_reason: '没有匹配的有效催化' },
  ];
  return { as_of_date: '2026-09-04', version: 'test', status: 'completed', notice: '仅供观察',
    coverage: { complete_history: 990, universe: 1000, verified_event_symbols: 1 }, lanes,
    report_bundle: { version: 'test', source_sha256: 'test', overlaps: [],
      reports: [{ key: 'overview', title: '所有策略总报告', filename: 'overview.md', markdown: '# 总报告', as_of_date: '2026-09-04', content_sha256: 'test' },
        ...lanes.map(l => ({ key: l.key, title: `${l.label}独立报告`, filename: `${l.key}.md`, markdown: `# ${l.label}正文`, as_of_date: '2026-09-04', content_sha256: 'test',
          result_summary: { key: l.key, label: l.label, conclusion: l.selected.length ? '本轮条件观察' : l.empty_reason,
            rows: l.selected.map(p => ({ ...p, state: '条件观察', conclusion: `${p.name}已完成研究结论` })) },
          review: { review_policy: '只讨论本策略', review_groups: [], review_plan: [],
            review_coverage: { planned: 0, completed: 0, missing_symbols: [], selected_reviewed: 0, selected_total: l.selected.length } } }))] } };
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('independent strategy reports', () => {
  it('filters paged history by the selected strategy and resets pages on strategy changes', async () => {
    const read = vi.spyOn(http, 'getJson').mockImplementation(async (path) => {
      const query = new URL(String(path), 'http://local').searchParams;
      return {detail:{status:'completed',total:90,page_total:0,items:[],note:`范围：${query.get('key') ?? '全策略'}`}} as never;
    });
    const scan=sample();scan.detail_run_id='fixed-run';scan.details_deferred=true;
    const wrapper=mount(ShortTermLanesPanel,{props:{summary:{strategy_lanes:scan}},global:{plugins:[ElementPlus]}});
    await wrapper.get('[data-report-key="expansion"]').trigger('click');
    await wrapper.findAll('.detail-controls button')[3]!.trigger('click');
    await vi.waitFor(()=>expect(wrapper.text()).toContain('范围：expansion'));
    expect(read.mock.calls.at(-1)?.[0]).toContain('key=expansion');
    expect(wrapper.findAll('.detail-controls button')[3]!.attributes('disabled')).toBeDefined();
    await wrapper.get('[data-report-key="pullback"]').trigger('click');
    expect(wrapper.text()).not.toContain('范围：expansion');
    await wrapper.findAll('.detail-controls button')[3]!.trigger('click');
    await vi.waitFor(()=>expect(wrapper.text()).toContain('范围：pullback'));
    expect(read.mock.calls.at(-1)?.[0]).toContain('key=pullback');
    expect(read.mock.calls.at(-1)?.[0]).toContain('offset=0');
    await wrapper.get('[data-report-key="overview"]').trigger('click');
    await wrapper.findAll('.detail-controls button')[3]!.trigger('click');
    await vi.waitFor(()=>expect(wrapper.text()).toContain('范围：全策略'));
    expect(read.mock.calls.at(-1)?.[0]).not.toContain('&key=');
    wrapper.unmount();
  });
  it('loads historical evidence only on request and pins it to the displayed run', async () => {
    const read = vi.spyOn(http, 'getJson').mockResolvedValue({detail: {status:'completed', total:0, items:[], note:'同轮记录'}});
    const scan = sample(); scan.detail_run_id = 'fixed-run'; scan.details_deferred = true;
    const wrapper = mount(ShortTermLanesPanel, {props:{summary:{strategy_lanes:scan}}, global:{plugins:[ElementPlus]}});
    expect(read.mock.calls.filter(c => c[0].includes('/post-close/detail'))).toHaveLength(0);
    await wrapper.findAll('.detail-controls button')[3]!.trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('同轮记录'));
    expect(read.mock.calls.some(c => c[0].includes('run_id=fixed-run') && c[0].includes('section=followup'))).toBe(true);
    wrapper.unmount();
  });
  it('renders the independent formal recommendation before the large scan payload arrives', () => {
    const recommendation = { status: 'ready', decision_id: 'decision-1', as_of_date: '2026-09-18', coverage: { candidates: 3, reviewed: 3, missing: [] }, recommended: [
      { symbol: '002008.SZ', name: '大族激光', priority: 2, stage: 'initial_breakout', sector: '自动化设备', business: '激光及自动化设备平台型公司', why_now: '放量突破', comparison: '同板块领先', trigger: '站稳确认', invalidation: '跌回平台', company_risk: '减持风险' },
    ] };
    const wrapper = mount(ShortTermLanesPanel, { props: { recommendation }, global: { plugins: [ElementPlus] } });
    expect(wrapper.text()).toContain('大族激光');
    expect(wrapper.text()).toContain('激光及自动化设备平台型公司');
    expect(wrapper.text()).toContain('多策略历史数据未齐');
    wrapper.unmount();
  });
  it('shows persisted conclusions and conditions before research process and data context', async () => {
    const wrapper = mount(ShortTermLanesPanel, { props: { summary: { strategy_lanes: sample() } }, global: { plugins: [ElementPlus] } });
    const overview = wrapper.get('[data-report-view="overview"]');
    expect(overview.findAll('h3')[0]!.text()).toBe('本次结论');
    expect(overview.text().indexOf('股票甲已完成研究结论')).toBeLessThan(overview.text().indexOf('策略对照与独立报告'));
    await wrapper.get('[data-report-key="expansion"]').trigger('click');
    const body = wrapper.get('[data-report-view="expansion"]');
    expect(body.findAll('h3')[0]!.text()).toBe('本次结论');
    expect(body.text().indexOf('股票甲已完成研究结论')).toBeLessThan(body.text().indexOf('为什么优先复核'));
    expect(body.text().indexOf('股票甲确认')).toBeLessThan(body.text().indexOf('数据与筛选说明'));
    await wrapper.get('[data-report-key="event"]').trigger('click');
    expect(wrapper.get('[data-result-summary="event"]').text()).toContain('没有匹配的有效催化');
    wrapper.unmount();
  });
  it('hydrates late, switches independently, and keeps an empty strategy visible', async () => {
    const wrapper = mount(ShortTermLanesPanel, { global: { plugins: [ElementPlus] } });
    expect(wrapper.find('[data-report-view]').exists()).toBe(false);
    await wrapper.setProps({ summary: { strategy_lanes: sample() } });
    expect(wrapper.findAll('[data-report-key]')).toHaveLength(4);
    expect(wrapper.find('[data-report-view="overview"]').text()).toContain('策略对照');
    expect(wrapper.find('[data-strategy-detail]').exists()).toBe(false);
    for (const [key, included, excluded] of [['expansion', '股票甲确认', '股票乙'], ['pullback', '股票乙确认', '股票甲']]) {
      await wrapper.find(`[data-report-key="${key}"]`).trigger('click');
      const body = wrapper.find(`[data-report-view="${key}"]`);
      expect(body.text()).toContain(included);
      expect(body.text()).not.toContain(excluded);
      expect(wrapper.findAll('[data-strategy-detail]')).toHaveLength(1);
    }
    await wrapper.find('[data-report-key="event"]').trigger('click');
    expect(wrapper.text()).toContain('没有匹配的有效催化');
    expect(wrapper.text()).toContain('已核验事件覆盖 1 只');
    const next = sample(); next.report_bundle!.reports = next.report_bundle!.reports.filter(r => r.key !== 'event');
    await wrapper.setProps({ summary: { strategy_lanes: next } });
    expect(wrapper.find('[data-report-view="overview"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('does not manufacture separate reports from legacy or missing data', () => {
    const scan = sample(); delete scan.report_bundle;
    const wrapper = mount(ShortTermLanesPanel, { props: { summary: { strategy_lanes: scan } }, global: { plugins: [ElementPlus] } });
    expect(wrapper.text()).toContain('尚无独立报告');
    expect(wrapper.find('[data-strategy-detail]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('downloads the exact saved body with its own filename', async () => {
    const create = vi.fn().mockReturnValue('blob:test'); const revoke = vi.fn();
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke });
    let filename = '';
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { filename = this.download; });
    const report = sample().report_bundle!.reports[2]!;
    downloadStrategyReport(report);
    expect(filename).toBe('pullback.md');
    const body = await new Promise<string>(resolve => {
      const reader = new FileReader(); reader.onload = () => resolve(String(reader.result));
      reader.readAsText(create.mock.calls[0]![0] as Blob);
    });
    expect(body).toBe(report.markdown);
    await vi.waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:test'), { timeout: 2000 });
  });
});

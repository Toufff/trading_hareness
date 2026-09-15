import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import StrategyGovernancePanel from './StrategyGovernancePanel.vue';

afterEach(() => { vi.unstubAllGlobals(); });
it('loads only when opened, hydrates late, and displays a read-only exact revision queue', async () => {
  let finish!: (value: Response) => void;
  const fetch = vi.fn(() => new Promise<Response>(resolve => { finish = resolve; }));
  vi.stubGlobal('fetch', fetch);
  const wrapper = mount(StrategyGovernancePanel);
  expect(fetch).not.toHaveBeenCalled();
  await wrapper.get('button').trigger('click');
  expect(wrapper.get('[role=status]').text()).toContain('正在读取');
  const hash = 'a'.repeat(64);
  finish(new Response(JSON.stringify({ status:'ready',notice:'不是盈利验证', counts:{ready:1},items:[{
    id:'PV02',title:'修复长上影误选',state:'ready',revision:7,issue:{problem:'未检查收盘位置',hypothesis:'加入区间位置'},
    current_experiment:0,experiments:[{spec:{change_kind:'ranking_config',validation_kind:'engineering'}}],
    reviews:[{actor:'independent-reviewer',payload:{dissent:'可能漏掉强势换手'}}],
    ready:{artifact_hash:hash,summary:'只修改启动',risk:'可能漏选',rollback:'恢复基线',approval_revision:7},
  }] }), { headers:{'content-type':'application/json'} }));
  await flushPromises();
  expect(wrapper.text()).toContain('修复长上影误选');
  expect(wrapper.text()).toContain('可能漏掉强势换手');
  expect(wrapper.text()).toContain('--revision 7');
  expect(wrapper.text()).toContain(hash);
  expect(wrapper.text()).toContain('已进入人工审核队列，尚未启用');
  expect(wrapper.text()).toContain('单日对照不等于策略成熟');
  expect(wrapper.findAll('button').map(button => button.text())).toEqual(['收起审查','刷新记录']);
  expect(fetch.mock.calls).toHaveLength(1);
  expect((fetch.mock.calls[0] as unknown[])[1]).not.toHaveProperty('method','POST');
  await wrapper.get('input[type=search]').setValue('不存在');
  expect(wrapper.text()).toContain('当前筛选下没有事项');
  wrapper.unmount();
});

it('never suggests the config activation command for code-release experiments', async () => {
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({status:'ok',items:[{
    id:'code',title:'代码修改',state:'ready',revision:7,current_experiment:0,
    ready:{artifact_hash:'a'.repeat(64)},experiments:[{spec:{change_kind:'code',release_plan:'人工发布新版本',candidate_manifest:'release.json'}}],
  }]}))));
  const wrapper=mount(StrategyGovernancePanel);await wrapper.get('button').trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('代码变更不支持通过配置 CLI 启用');
  expect(wrapper.text()).not.toContain('strategy-governance.py activate');wrapper.unmount();
});

it('surfaces the actual blocked owner and next action without implying a model or repair ran', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({status:'ok',items:[{
    id:'blocked',title:'待检验改进',state:'reviewed',revision:2,latest_diagnostic:{status:'preflight_blocked',
      role:'proposer',system_owner:'运行调度器',reason:'缺少可用的输入快照',next_step:'补齐输入后重新排队',model_started:false,recorded_at:'2026-09-11T10:00:00Z'},
  }]}))));
  const wrapper=mount(StrategyGovernancePanel); await wrapper.get('button').trigger('click'); await flushPromises();
  const diagnostic=wrapper.get('[aria-label="最新执行诊断"]');
  expect(diagnostic.text()).toContain('缺少可用的输入快照');
  expect(diagnostic.text()).toContain('系统负责角色：运行调度器');
  expect(diagnostic.text()).toContain('补齐输入后重新排队');
  expect(diagnostic.text()).toContain('本次是否调用模型：否');
  expect(diagnostic.text()).toContain('不代表已完成修复');
  expect(wrapper.text()).toContain('问题已复核'); wrapper.unmount();
});

it('keeps network errors separate from empty results and aborts on unmount', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('<html>bad gateway</html>',{status:502}));
  vi.stubGlobal('fetch',fetch);
  const wrapper = mount(StrategyGovernancePanel);
  await wrapper.get('button').trigger('click'); await flushPromises();
  expect(wrapper.get('[role=alert]').text()).toContain('非 JSON');
  expect(wrapper.text()).not.toContain('尚无改进记录');
  const signal = fetch.mock.calls[0]![1].signal as AbortSignal;
  wrapper.unmount(); expect(signal.aborted).toBe(true);
});

it('renders empty evidence honestly and uses accessible wrap-friendly mobile controls', async () => {
  vi.stubGlobal('innerWidth',375);
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({status:'completed',items:[],counts:{}}))));
  const wrapper = mount(StrategyGovernancePanel);
  expect(wrapper.get('section').attributes('aria-label')).toBe('策略治理与实验验收');
  await wrapper.get('button').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('不代表策略已经通过验收');
  expect(wrapper.find('label input[type=search]').exists()).toBe(true);
  expect(wrapper.find('table').exists()).toBe(false);
  wrapper.unmount();
});

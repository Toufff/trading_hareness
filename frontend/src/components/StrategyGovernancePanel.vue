<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { evidenceText, governanceLabel, loadGovernance, type GovernanceItem, type GovernanceProjection } from './strategy-governance';

const expanded = ref(false);
const loading = ref(false);
const error = ref('');
const payload = ref<GovernanceProjection>();
const query = ref('');
const readyOnly = ref(false);
let controller: AbortController | undefined;
const isReady = (state: string) => ['ready', 'ready_for_release', 'ready_for_human', 'ready_for_promotion'].includes(state);
const experimentSpec = (item: GovernanceItem) => item.current_experiment == null ? undefined : item.experiments?.[item.current_experiment]?.spec;
const configActivation = (item: GovernanceItem) => experimentSpec(item) && (experimentSpec(item)?.change_kind ?? 'ranking_config') === 'ranking_config';
const items = computed(() => (payload.value?.items ?? []).filter(item =>
  (!readyOnly.value || isReady(String(item.status ?? item.state ?? ''))) &&
  `${item.title ?? ''} ${item.item_id ?? item.id ?? ''}`.toLowerCase().includes(query.value.toLowerCase())));
async function refresh() {
  controller?.abort(); controller = new AbortController(); const own = controller;
  loading.value = true; error.value = ''; payload.value = undefined;
  try { const value = await loadGovernance(own.signal); if (!own.signal.aborted) payload.value = value; }
  catch (cause) { if (!own.signal.aborted) error.value = cause instanceof Error ? cause.message : String(cause); }
  finally { if (!own.signal.aborted) loading.value = false; }
}
function toggle() { expanded.value = !expanded.value; if (expanded.value && !payload.value && !loading.value) void refresh(); }
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <section class="governance" aria-label="策略治理与实验验收">
    <header>
      <div><h2>策略治理与实验验收</h2><p>原子改进、独立质疑、隔离实验；未经人工批准，不改变正式策略。</p></div>
      <button type="button" :aria-expanded="expanded" @click="toggle">{{ expanded ? '收起审查' : '查看改进与待落地队列' }}</button>
    </header>
    <div v-if="expanded" class="governance-body">
      <aside class="boundary">实现检查通过不等于盈利有效。此页只读，没有启用、审批或部署按钮；人工批准必须绑定精确修订与工件。</aside>
      <div class="toolbar">
        <label>查找事项<input v-model="query" type="search" placeholder="标题或事项编号" /></label>
        <label class="check"><input v-model="readyOnly" type="checkbox" />只看待人工落地</label>
        <button type="button" :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '刷新记录' }}</button>
      </div>
      <p v-if="loading" role="status">正在读取已保存的治理证据，不会启动实验。</p>
      <p v-else-if="error" role="alert" class="error">治理记录读取失败：{{ error }}。不能据此判断没有问题或已验收。</p>
      <template v-else-if="payload">
        <p class="notice">{{ payload.notice }}</p>
        <p v-if="payload.truncated" role="status">当前仅显示最近 {{ payload.items.length }} 项，计数包含全部记录；列表不是完整历史。</p>
        <p v-if="payload.active" class="identity">最近人工生效记录：{{ payload.active.item_id }} · 第 {{ payload.active.generation }} 次变更 · {{ payload.active.recorded_at }} · {{ governanceLabel(payload.active.action) }}。历史启用记录不代表实验自动上线。</p>
        <div class="counts"><span v-for="(count, state) in payload.counts" :key="state">{{ governanceLabel(state) }} <strong>{{ count }}</strong></span></div>
        <p v-if="!items.length" role="status">{{ payload.items.length ? '当前筛选下没有事项。' : '尚无改进记录；这不代表策略已经通过验收。' }}</p>
        <article v-for="(item, index) in items" :key="String(item.item_id ?? item.id ?? index)" class="issue">
          <header><h3>{{ item.title ?? '未命名改进事项' }}</h3><span class="state">{{ governanceLabel(String(item.status ?? item.state ?? '未提供状态')) }}</span></header>
          <p class="identity">事项 {{ item.item_id ?? item.id ?? '未提供' }} · 修订 {{ item.revision ?? '未提供' }}</p>
          <aside v-if="item.latest_diagnostic" class="boundary diagnostic" aria-label="最新执行诊断">
            <strong>最新执行诊断：{{ governanceLabel(item.latest_diagnostic.status ?? '未提供状态') }}</strong>
            <p>原因：{{ item.latest_diagnostic.reason ?? '未提供' }}</p>
            <p>系统负责角色：{{ governanceLabel(item.latest_diagnostic.system_owner ?? item.latest_diagnostic.role ?? '未提供') }}</p>
            <p>下一步处理：{{ item.latest_diagnostic.next_step ?? '未提供，需补充诊断' }}</p>
            <p>本次是否调用模型：{{ item.latest_diagnostic.model_started == null ? '未记录' : item.latest_diagnostic.model_started ? '是' : '否' }} · {{ item.latest_diagnostic.recorded_at ?? '时间未记录' }}</p>
            <p>诊断不改变事项阶段，也不代表已完成修复或验证。</p>
          </aside>
          <p v-if="experimentSpec(item)?.validation_kind === 'engineering'" class="boundary"><strong>本项仅完成或正在进行工程行为验证。</strong>候选资格、排序范围和代码行为检查不代表收益、胜率或实盘可成交性已验证；单日对照不等于策略成熟。</p>
          <p v-if="item.issue?.problem ?? item.problem"><strong>问题：</strong>{{ evidenceText(item.issue?.problem ?? item.problem) }}</p>
          <p v-if="item.issue?.hypothesis ?? item.hypothesis"><strong>改进假设：</strong>{{ evidenceText(item.issue?.hypothesis ?? item.hypothesis) }}</p>
          <dl><div><dt>精确工件哈希</dt><dd><code>{{ item.ready?.artifact_hash ?? item.artifact_hash ?? item.artifact_sha256 ?? '尚未形成待批准工件，不可批准' }}</code></dd></div></dl>
          <details><summary>查看范围、复核分歧、实验与修订证据</summary>
            <dl class="evidence"><div v-for="(value, key) in item" :key="key"><dt>{{ governanceLabel(String(key)) }}</dt><dd>{{ evidenceText(value) }}</dd></div></dl>
          </details>
          <aside v-if="isReady(String(item.status ?? item.state ?? ''))" class="boundary">
            <strong>已进入人工审核队列，尚未启用。</strong>
            <p>{{ item.ready?.summary }}</p><p>风险：{{ item.ready?.risk ?? '未提供，不应批准' }}</p><p>回退：{{ item.ready?.rollback ?? '未提供，不应批准' }}</p>
            <p>审核有效期：{{ item.ready?.expires_at ?? '未提供' }}。先复核上述证据，再在本机仓库目录执行；网页不会代为执行命令。</p>
            <code v-if="item.ready?.artifact_hash && configActivation(item)">python scripts/strategy-governance.py activate {{ item.id }} --revision {{ item.revision }} --artifact-hash {{ item.ready.artifact_hash }} --actor &lt;已登记的人工身份&gt;</code>
            <template v-else-if="experimentSpec(item)?.change_kind === 'code'"><p><strong>代码变更不支持通过配置 CLI 启用。</strong>必须独立进行人工审查的发布流程；进入此队列不代表代码已部署。</p><p>发布方案：{{ experimentSpec(item)?.release_plan ?? '未提供' }}</p><p>候选清单：{{ experimentSpec(item)?.candidate_manifest ?? '未提供' }}</p></template>
            <p v-else>缺少支持的变更类型或冻结工件，不能给出启用命令。</p>
          </aside>
        </article>
      </template>
    </div>
  </section>
</template>

<style scoped>
.governance{margin:0 0 20px;border:1px solid var(--el-border-color,#dce3ed);border-radius:12px;background:var(--el-bg-color,#fff);padding:20px;color:var(--el-text-color-primary,#263448);min-width:0}.governance header{display:flex;gap:16px;justify-content:space-between;align-items:center}.governance h2{font-size:18px;margin:0 0 6px}.governance h3{font-size:15px;margin:0}.governance p{font-size:13px;line-height:1.7;margin:8px 0}.governance header p,.identity,.notice{color:var(--el-text-color-secondary,#66758b)}.governance button{border:1px solid var(--el-color-primary,#347de0);border-radius:6px;padding:9px 13px;background:var(--el-bg-color,#fff);color:var(--el-color-primary,#347de0);cursor:pointer;flex-shrink:0}.governance button:disabled{opacity:.6;cursor:wait}.governance-body{margin-top:18px}.boundary{background:var(--el-fill-color-light,#f3f6fa);border-left:3px solid #738eac;padding:12px 14px;line-height:1.7;font-size:13px}.toolbar{display:flex;flex-wrap:wrap;gap:16px;align-items:end;margin:16px 0}.toolbar label{font-size:13px;display:flex;gap:7px;flex-direction:column}.toolbar .check{flex-direction:row;align-items:center;padding-bottom:8px}.toolbar input[type=search]{padding:8px;border:1px solid var(--el-border-color,#dce3ed);border-radius:5px;background:var(--el-bg-color,#fff);color:inherit;max-width:100%}.counts{display:flex;flex-wrap:wrap;gap:8px;font-size:12px}.counts span,.state{background:var(--el-fill-color-light,#f3f6fa);padding:5px 9px;border-radius:4px;font-size:12px}.issue{border-top:1px solid var(--el-border-color,#dce3ed);margin-top:18px;padding-top:18px;min-width:0}.issue dl{font-size:12px;line-height:1.7}.issue dt{font-weight:600;margin-top:8px}.issue dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.issue code{overflow-wrap:anywhere}.issue summary{font-size:13px;cursor:pointer;color:var(--el-color-primary,#347de0);padding:8px 0}.issue .boundary{margin-top:10px}.error{color:var(--el-color-danger,#ba3b3b)}@media(max-width:640px){.governance{padding:14px}.governance header{align-items:flex-start;flex-direction:column}.toolbar{align-items:stretch;flex-direction:column}.toolbar input[type=search]{width:100%;box-sizing:border-box}.governance header button{width:100%}.issue header{gap:8px}}
</style>

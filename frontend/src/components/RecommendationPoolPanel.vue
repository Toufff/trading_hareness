<script setup lang="ts">
import { computed } from 'vue';
type Pick = { symbol: string; name: string; priority: number; stage: string; why_now: string; comparison: string; trigger: string; invalidation: string; company_risk: string };
type Decision = { status: string; decision_id?: string; as_of_date?: string; market_assessment?: string; notice?: string; recommended?: Pick[]; coverage?: { candidates: number; reviewed: number; missing: string[]; errors?: Record<string,string> }; reviewed?: (Pick & { decision: string })[] };
const props = defineProps<{ value?: unknown }>();
const decision = computed(() => props.value as Decision | undefined);
const stageNames: Record<string,string> = { accumulation: '横盘潜伏', initial_breakout: '初步启动', strong_pullback: '强势回踩', post_limit: '涨停后承接', other: '其他观察' };
const download = () => {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(decision.value, null, 2)], { type: 'application/json' }));
  link.download = `recommendation-${decision.value?.as_of_date}.json`; link.click(); URL.revokeObjectURL(link.href);
};
</script>
<template>
  <section class="recommendation-decision" aria-label="正式推荐决策" :data-decision-id="decision?.decision_id">
    <header><div><h2>本轮重点与观察</h2><p>与持仓独立 · 来自同一份正式决策，不按聊天记录拼名单</p></div><button v-if="decision?.decision_id" @click="download">下载决策凭据</button></header>
    <p v-if="decision?.status !== 'ready'" role="status" class="warning">{{ decision?.notice || '本轮推荐决策尚未发布；下面的策略名单仅为扫描候选。' }}</p>
    <template v-if="decision?.decision_id">
      <p v-if="decision.coverage?.missing?.length" class="warning">本轮必核缺项：{{ decision.coverage.missing.join('、') }}。有效研究仍展示，不能据此覆盖完整推荐组。</p>
      <p v-for="(reason, symbol) in decision.coverage?.errors" :key="symbol" class="warning">{{ symbol }}：{{ reason }}</p>
      <p>{{ decision.market_assessment }}</p>
      <p class="meta">{{ decision.as_of_date }} · {{ decision.status }} · 完整候选 {{ decision.coverage?.candidates }}，完成深入复核 {{ decision.coverage?.reviewed }} · 编号 {{ decision.decision_id.slice(0, 12) }}</p>
      <div class="picks"><article v-for="p in decision.recommended" :key="p.symbol">
        <h3>{{ p.priority }}. {{ p.name }}（{{ p.symbol.split('.')[0] }}）</h3><span class="stage">{{ stageNames[p.stage] || p.stage }}</span>
        <p>{{ p.why_now }}</p><p><strong>为什么优先：</strong>{{ p.comparison }}</p>
        <p><strong>观察触发：</strong>{{ p.trigger }}</p><p><strong>取消条件：</strong>{{ p.invalidation }}</p>
        <details><summary>公司风险</summary><p>{{ p.company_risk }}</p></details>
      </article></div>
      <details><summary>其他完成复核的股票与取舍</summary><p v-for="p in decision.reviewed?.filter(x => x.decision !== 'recommend')" :key="p.symbol"><strong>{{ p.name }}：</strong>{{ p.comparison }}</p></details>
      <p class="meta">{{ decision.notice }}</p>
    </template>
  </section>
</template>
<style scoped>
.recommendation-decision{margin:0 0 24px;padding:22px;border:1px solid #d8e2ee;border-radius:12px;background:#f8fbff;color:#203149}.recommendation-decision header{display:flex;justify-content:space-between;gap:18px;align-items:start}.recommendation-decision h2{margin:0}.recommendation-decision p{line-height:1.7}.meta,header p{color:#61738a;font-size:13px;overflow-wrap:anywhere}.picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:14px}.picks article{padding:18px;background:white;border:1px solid #e0e7ef;border-radius:9px}.picks h3{margin:0 0 12px}.stage{font-size:12px;background:#e7efff;color:#234c92;padding:4px 8px;border-radius:5px}.warning{background:#fff1d9;padding:12px;color:#8b5010}.recommendation-decision button{padding:7px 12px;white-space:nowrap;border:1px solid #c6d3e4;background:white;border-radius:5px;cursor:pointer}details{margin-top:14px}summary{cursor:pointer}@media(max-width:600px){.recommendation-decision{padding:14px}.recommendation-decision header{display:block}}
</style>

<script setup lang="ts">
type Review = { symbol: string; name: string; business: string; risk: string; conclusion: string;
  selection_reason: string; outcome_label: string; selection?: { why_now: string; question: string };
  sources: { url: string; published_date: string }[] };
type Group = { key: string; label: string; items: Review[] };
export type ReviewProjection = { review_policy?: string; review_groups?: Group[];
  review_coverage?: { planned: number; completed: number; missing_symbols: string[] };
  review_plan?: { symbol: string; name: string; selection_reason: string }[] };
defineProps<{ scan: ReviewProjection }>();
</script>

<template>
  <section v-if="scan.review_groups" class="review-selection">
    <h3>为什么优先复核这些股票</h3>
    <p class="policy">{{ scan.review_policy }}</p>
    <p v-if="scan.review_coverage" class="policy">计划 {{ scan.review_coverage.planned }} 只，已完成公司证据复核 {{ scan.review_coverage.completed }} 只。复核完成不等于买入授权。</p>
    <template v-for="target in scan.review_plan" :key="target.symbol">
      <p v-if="scan.review_coverage?.missing_symbols.includes(target.symbol)" class="missing">{{ target.name }}（{{ target.symbol.split('.')[0] }}）：{{ target.selection_reason }} 本轮未取得有效公司复核结论，不能用其他股票冒充完成。</p>
    </template>
    <template v-for="group in scan.review_groups" :key="group.key">
      <component :is="group.key === 'background' ? 'details' : 'section'" v-if="group.items.length" class="review-group" :data-review-group="group.key">
        <component :is="group.key === 'background' ? 'summary' : 'h3'">{{ group.label }}</component>
        <article v-for="review in group.items" :key="review.symbol">
          <h4>{{ review.name }}（{{ review.symbol.split('.')[0] }}）<small>{{ review.outcome_label }}</small></h4>
          <p><strong>为什么复核：</strong>{{ review.selection_reason }}</p>
          <p><strong>具体核查：</strong>{{ review.selection?.question ?? '旧记录未记录，不补造' }}</p>
          <p><strong>结论：</strong>{{ review.conclusion }}</p>
          <details><summary>主营、研究动机、风险与出处</summary>
            <p>主营：{{ review.business }}</p><p>研究动机：{{ review.selection?.why_now ?? '旧记录未记录' }}</p><p>风险：{{ review.risk }}</p>
            <p v-for="source in review.sources" :key="source.url"><a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.published_date }} 公司公告</a></p>
          </details>
        </article>
      </component>
    </template>
  </section>
</template>

<style scoped>
.review-selection{margin-bottom:20px}.policy{font-size:12px;color:var(--el-text-color-secondary);line-height:1.8}.review-group{padding:16px;border:1px solid var(--el-border-color-lighter);border-radius:10px;margin-top:14px}.review-group h3{margin:0;font-size:15px}.review-group article{padding:14px 0;border-top:1px solid var(--el-border-color-lighter);margin-top:12px}.review-group h4{margin:0 0 8px;font-size:14px}.review-group small{margin-left:12px;color:var(--el-color-primary);font-weight:normal}.review-group p,.missing{font-size:13px;line-height:1.8;margin:8px 0;white-space:pre-line}.review-group summary{cursor:pointer;color:var(--el-color-primary);font-size:12px}.missing{color:var(--el-color-warning-dark-2)}
</style>

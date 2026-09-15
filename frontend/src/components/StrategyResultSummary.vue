<script setup lang="ts">
import type { StrategyResult } from './short-term-reports';
defineProps<{ result: StrategyResult; compact?: boolean }>();
</script>

<template>
  <section class="result-summary" :data-result-summary="result.key">
    <h3>{{ compact ? result.label : '本次结论' }}</h3>
    <p class="conclusion">{{ result.conclusion }}</p>
    <article v-for="row in result.rows" :key="row.symbol">
      <h4>{{ row.name }}（{{ row.symbol.split('.')[0] }}）<small>{{ row.state }}</small></h4>
      <p><strong>结论：</strong>{{ row.conclusion }}</p>
      <template v-if="!compact">
        <p><strong>为什么关注：</strong>{{ row.reason }}</p>
        <p><strong>确认条件：</strong>{{ row.confirmation || '本轮未形成入场条件' }}</p>
        <p><strong>放弃条件：</strong>{{ row.invalidation || '本轮未形成失效条件' }}</p>
        <p class="boundary">{{ row.caution }} · 有效期：{{ row.expiry }}</p>
      </template>
    </article>
  </section>
</template>

<style scoped>
.result-summary{padding:18px 20px;margin:16px 0;border:1px solid var(--el-border-color-lighter);border-radius:10px;background:var(--el-fill-color-extra-light)}h3{margin:0 0 12px;font-size:17px}.conclusion{font-weight:500}p{font-size:13px;line-height:1.85;white-space:pre-line}article{border-top:1px solid var(--el-border-color-lighter);padding-top:14px;margin-top:14px}h4{margin:0;font-size:15px}small{margin-left:10px;font-weight:400;color:var(--el-text-color-secondary)}.boundary{color:var(--el-text-color-secondary);font-size:12px}
</style>

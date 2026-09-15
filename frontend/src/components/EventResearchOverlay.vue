<script setup lang="ts">
import EventResearchLive from './EventResearchLive.vue';
import EventResearchPanel, {type EventResearch} from './EventResearchPanel.vue';
defineProps<{historical?:EventResearch;symbols?:string[]}>();
</script>
<template>
  <section>
    <h3>最新消息对当前观察的影响</h3>
    <p>以下按最新消息时点更新观察处理；不改写本报告的历史行情、排名或交易计划。宏观消息与本策略涉及个股分别关联。</p>
    <EventResearchLive :symbols="symbols" />
    <details><summary>查看扫描当时的消息依据（历史快照）</summary>
      <EventResearchPanel :value="historical" :symbols="symbols" />
    </details>
  </section>
</template>
<style scoped>p{font-size:13px;line-height:1.7;color:var(--el-text-color-secondary)}summary{cursor:pointer;font-size:13px}</style>

<script setup lang="ts">
/**
 * Fixed-text marker for any panel that surfaces a candidate, recommendation
 * or signal.  It exists purely to stop a research-only list from being read
 * as an executable trade instruction; the wording is intentionally not
 * configurable so every call site carries the same boundary.
 */
</script>

<template>
  <el-tag role="note" type="warning" effect="plain" size="small" class="research-only-badge">
    仅供研究参考，非交易建议
  </el-tag>
</template>

<style scoped>
.research-only-badge {
  white-space: normal;
  height: auto;
  line-height: 1.4;
  padding: 2px 8px;
}
</style>

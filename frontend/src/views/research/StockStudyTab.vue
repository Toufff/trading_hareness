<script setup lang="ts">
import { inject } from 'vue';
import StockResearchWorkbench from '../../components/StockResearchWorkbench.vue';
import { dashboardContextKey } from '../../dashboard-context';

const dashboard = inject(dashboardContextKey);
if (!dashboard) throw new Error('research tab requires the dashboard shell context');
</script>

<template>
  <el-card shadow="never" class="study-launcher">
    <el-form inline @submit.prevent="dashboard.runStockStudy">
      <el-form-item label="股票代码">
        <el-input v-model="dashboard.studySymbol" placeholder="600487.SH" clearable />
      </el-form-item>
      <el-form-item label="历史窗口">
        <el-input-number v-model="dashboard.studyLookback" :min="30" :max="300" :step="30" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="dashboard.studyLoading" @click="dashboard.runStockStudy">
          打开策略工作台
        </el-button>
      </el-form-item>
    </el-form>
    <p class="launcher-note">
      日线是公共底座；资金、换手、事件与技术指标由所选策略按需展示。单次历史请求最多 300 根，超出范围不会静默截断。
    </p>
    <el-alert v-if="dashboard.studyError" :title="dashboard.studyError" type="error" :closable="false" show-icon />
  </el-card>

  <StockResearchWorkbench
    v-if="dashboard.stockStudy"
    :workbench="dashboard.stockStudy"
    :control="dashboard.stockWorkbenchControl"
    class="section-gap"
  />
  <el-empty v-else-if="!dashboard.studyLoading" description="输入股票代码后打开策略研究工作台" :image-size="88" />
</template>

<style scoped>
.study-launcher { margin-bottom: 14px; }
.study-launcher :deep(.el-card__body) { padding-bottom: 12px; }
.launcher-note { margin: -4px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
</style>

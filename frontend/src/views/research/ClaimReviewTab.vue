<script lang="ts">
import { defineComponent, inject } from 'vue';
import { Refresh, WarningFilled } from '@element-plus/icons-vue';
import VChart from 'vue-echarts';
import { dashboardContextKey } from '../../dashboard-context';

export default defineComponent({
  name: 'ClaimReviewTab',
  components: { Refresh, VChart, WarningFilled },
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('research tab requires the dashboard shell context');
    return dashboard as Record<string, any>;
  },
});
</script>

<template>

  <el-card shadow="never" header="待映射分析师标的"><el-alert title="无法精确识别为股票代码的远端文本必须人工映射后才能参与股票级评分。" type="warning" :closable="false" show-icon/><el-table :data="claimReviews" max-height="560" class="section-gap"><el-table-column prop="analyst_name" label="分析师" width="120"/><el-table-column prop="suggested_label" label="原始标的"/><el-table-column label="方向" width="80"><template #default="{ row }"><el-tag :type="recommendationType(row.direction)">{{ claimDirection(row.direction) }}</el-tag></template></el-table-column><el-table-column prop="horizon_days" label="周期" width="80"/><el-table-column label="映射代码" width="160"><template #default="{ row }"><el-input v-model="reviewSymbol[row.review_id]" :placeholder="row.suggested_symbol || '000636.SZ'"/></template></el-table-column><el-table-column prop="evidence" label="证据" show-overflow-tooltip/><el-table-column label="操作" width="160"><template #default="{ row }"><el-button link type="success" @click="decideReview(row,'approved')">批准</el-button><el-button link type="danger" @click="decideReview(row,'rejected')">拒绝</el-button></template></el-table-column></el-table></el-card>

</template>

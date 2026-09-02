<script lang="ts">
import { defineComponent, inject } from 'vue';
import { dashboardContextKey } from '../../dashboard-context';
import ResearchOnlyBadge from '../../components/ResearchOnlyBadge.vue';

export default defineComponent({
  name: 'StrategyTab',
  components: { ResearchOnlyBadge },
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('research tab requires the dashboard shell context');
    return dashboard;
  },
});
</script>

<template>

  <el-row :gutter="14">
    <el-col :md="9" :xs="24"><el-card shadow="never" header="核心股票池"><el-form label-position="top"><el-form-item label="股票代码"><el-input v-model="universeText" type="textarea" :rows="4" placeholder="000636.SZ, 603580.SH"/></el-form-item><el-form-item label="优先级"><el-input-number v-model="universePriority" :min="1" :max="10000"/></el-form-item><el-button type="primary" :loading="actionLoading === '更新核心股票池'" @click="saveUniverse">保存股票池</el-button></el-form><el-table :data="universe" size="small" max-height="250" class="section-gap"><el-table-column prop="symbol" label="代码"/><el-table-column prop="name" label="名称"/><el-table-column prop="priority" label="优先级" width="82"/></el-table></el-card></el-col>
    <el-col :md="15" :xs="24"><el-card shadow="never"><template #header><div class="card-header"><el-space><span>方向推荐</span><ResearchOnlyBadge /></el-space><el-space><el-button :loading="actionLoading === '构建多源特征'" @click="runAction('构建多源特征','/api/research/features/build',{ universe_key: 'core' })">构建特征</el-button><el-button type="primary" :loading="actionLoading === '生成方向推荐'" @click="runAction('生成方向推荐','/api/research/recommendations/generate',{ universe_key: 'core', horizon_days: 20 })">生成推荐</el-button></el-space></div></template><el-alert title="推荐基于已落库的多源证据、技术趋势、资金流与已审核分析师观点；仅供研究，不自动下单。" type="info" :closable="false" show-icon/><el-table :data="recommendations" max-height="420" class="section-gap"><el-table-column prop="rank" label="#" width="52"/><el-table-column prop="symbol" label="标的"/><el-table-column label="方向" width="82"><template #default="{ row }"><el-tag :type="recommendationType(row.direction)">{{ recommendationDirection(row.direction) }}</el-tag></template></el-table-column><el-table-column prop="score" label="评分" width="75"/><el-table-column label="置信度" width="92"><template #default="{ row }">{{ row.confidence === undefined ? '-' : `${Math.round(row.confidence * 100)}%` }}</template></el-table-column><el-table-column prop="horizon_days" label="周期" width="72"/><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="row.decision === 'research_candidate' ? 'success' : row.decision === 'no_trade' ? 'danger' : 'info'">{{ row.decision }}</el-tag></template></el-table-column><el-table-column label="风险" min-width="160"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.risk_flags ?? []" :key="flag" size="small" type="warning">{{ flag }}</el-tag></el-space></template></el-table-column></el-table></el-card></el-col>
  </el-row>
  <el-card shadow="never" header="特征证据"><el-table :data="featureItems" max-height="360"><el-table-column prop="symbol" label="标的" width="120"/><el-table-column prop="name" label="名称" width="120"/><el-table-column label="收盘" width="95"><template #default="{ row }">{{ displayValue(row.features.close) }}</template></el-table-column><el-table-column label="5日收益" width="110"><template #default="{ row }">{{ displayValue(row.features.return_5) }}</template></el-table-column><el-table-column label="20日收益" width="110"><template #default="{ row }">{{ displayValue(row.features.return_20) }}</template></el-table-column><el-table-column label="东财主力占比" width="130"><template #default="{ row }">{{ displayValue(featureRecord(row,'moneyflow_dc').net_amount_rate) }}</template></el-table-column><el-table-column label="分析师共识" width="125"><template #default="{ row }">{{ displayValue(featureRecord(row,'analyst').consensus) }}</template></el-table-column><el-table-column label="质量标记" min-width="180"><template #default="{ row }"><el-space wrap><el-tag v-for="flag in row.quality_flags" :key="flag" size="small" type="warning">{{ flag }}</el-tag></el-space></template></el-table-column></el-table></el-card>

</template>

<script lang="ts">
import { defineComponent, inject } from 'vue';
import { Refresh } from '@element-plus/icons-vue';
import { dashboardContextKey } from '../../dashboard-context';

export default defineComponent({
  name: 'CatalogTab',
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('research tab requires the dashboard shell context');
    return { ...dashboard, Refresh };
  },
});
</script>

<template>

  <el-row :gutter="14" class="metric-row">
    <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>接口库存</span><strong>{{ catalogCount('total') }}</strong></el-card></el-col>
    <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>官方积分扩展</span><strong>{{ catalogCount('points_at_or_below_15000') }}</strong></el-card></el-col>
    <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>实时接口</span><strong>{{ catalogCount('market_hours_only') }}</strong></el-card></el-col>
    <el-col :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>主 / SDK / GET 验证</span><strong>{{ catalogCount('primary_verified') }} / {{ catalogCount('super_sdk_verified') }} / {{ catalogCount('super_get_verified') }}</strong></el-card></el-col>
  </el-row>
  <el-card shadow="never">
    <template #header><div class="card-header"><span>物理通道权限与完整性矩阵</span><el-space><el-button :icon="Refresh" :loading="catalogRefreshing" @click="refreshCatalog">刷新状态</el-button><el-button :disabled="!selectedCatalog.length" :loading="actionLoading === '核验所选接口'" @click="auditSelectedCatalog">核验所选（{{ selectedCatalog.length }}）</el-button><el-button type="primary" @click="openFetch()">读取数据</el-button></el-space></div></template>
    <el-alert title="目录登记不等于可用，返回成功也不等于完整。主源、Super SDK、Super GET 分开记账；GET 的 ths_member 仅限小结果，完整板块快照固定走 SDK。" type="info" :closable="false" show-icon class="section-gap"/>
    <div class="table-toolbar"><el-input v-model="catalogQuery" placeholder="搜索 API、类别或模型用途" clearable/><el-select v-model="catalogGroup"><el-option v-for="group in catalogGroups" :key="group" :label="group === 'all' ? '全部类别' : group" :value="group"/></el-select><el-tag type="info">{{ visibleCatalog.length }} / {{ catalog.count ?? 0 }}</el-tag><el-tag type="warning">历史分钟 {{ catalogCount('offline_files_only') }} 项仅文件导入</el-tag></div>
    <el-table v-if="!mobileLayout" :data="visibleCatalog" row-key="api_name" max-height="560" @selection-change="selectCatalog">
      <el-table-column type="selection" width="40"/>
      <el-table-column prop="api_name" label="API" width="140" fixed/>
      <el-table-column label="分类 / 模型用途" min-width="190"><template #default="{ row }"><div class="catalog-purpose"><span>{{ row.group }}</span><small>{{ row.model_role }}</small></div></template></el-table-column>
      <el-table-column label="权限" width="92"><template #default="{ row }"><el-tag size="small" type="info">{{ permissionText(row) }}</el-tag></template></el-table-column>
      <el-table-column label="策略" width="96"><template #default="{ row }"><el-tag size="small" :type="row.request_policy === 'market_hours_only' ? 'warning' : row.request_policy === 'offline_files_only' ? 'info' : 'success'">{{ policyText(row.request_policy) }}</el-tag></template></el-table-column>
      <el-table-column label="主源" width="112"><template #default="{ row }"><el-tag size="small" :type="availabilityType(row.primary_availability)">{{ availabilityText(row.primary_availability) }}</el-tag><small class="catalog-check-time">{{ observationText(row, 'tushare_primary') }}</small></template></el-table-column>
      <el-table-column label="Super SDK" width="112"><template #default="{ row }"><el-tag size="small" :type="availabilityType(row.super_sdk_availability)">{{ availabilityText(row.super_sdk_availability) }}</el-tag><small class="catalog-check-time">{{ observationText(row, 'tushare_super_sdk') }}</small></template></el-table-column>
      <el-table-column label="Super GET" width="112"><template #default="{ row }"><el-tag size="small" :type="availabilityType(row.super_get_availability)">{{ availabilityText(row.super_get_availability) }}</el-tag><small class="catalog-check-time">{{ observationText(row, 'tushare_super_get') }}</small></template></el-table-column>
      <el-table-column label="入模" width="82"><template #default="{ row }"><el-tag :type="row.normalized ? 'success' : 'info'">{{ row.normalized ? '标准化' : '仅证据' }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="64" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openFetch(row)">读取</el-button></template></el-table-column>
    </el-table>
    <el-table v-else :data="visibleCatalog" row-key="api_name" max-height="520" size="small" @selection-change="selectCatalog">
      <el-table-column type="selection" width="36"/>
      <el-table-column label="API / 权限" min-width="96"><template #default="{ row }"><div class="catalog-mobile-api"><strong>{{ row.api_name }}</strong><small>{{ permissionText(row) }}</small></div></template></el-table-column>
      <el-table-column label="物理通道" width="104"><template #default="{ row }"><div class="catalog-mobile-status"><el-tag size="small" :type="availabilityType(row.primary_availability)">主 {{ availabilityText(row.primary_availability) }}</el-tag><el-tag size="small" :type="availabilityType(row.super_sdk_availability)">SDK {{ availabilityText(row.super_sdk_availability) }}</el-tag><el-tag size="small" :type="availabilityType(row.super_get_availability)">GET {{ availabilityText(row.super_get_availability) }}</el-tag></div></template></el-table-column>
      <el-table-column label="操作" width="52"><template #default="{ row }"><el-button link type="primary" @click="openFetch(row)">读取</el-button></template></el-table-column>
    </el-table>
  </el-card>
  <el-card v-if="auditResults.length" shadow="never" header="最近物理通道核验结果"><el-table :data="auditResults" max-height="360" size="small"><el-table-column prop="api_name" label="API" width="170"/><el-table-column prop="provider" label="来源" width="110"/><el-table-column label="状态" width="112"><template #default="{ row }"><el-tag :type="availabilityType(row.availability)">{{ availabilityText(row.availability) }}</el-tag></template></el-table-column><el-table-column prop="received" label="响应" width="75"/><el-table-column prop="stored" label="证据" width="75"/><el-table-column prop="reason" label="说明" show-overflow-tooltip/></el-table></el-card>

</template>

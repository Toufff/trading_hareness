<script lang="ts">
import { defineComponent, inject } from 'vue';
import { dashboardContextKey } from '../../dashboard-context';

export default defineComponent({
  name: 'QualityTab',
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('research tab requires the dashboard shell context');
    return dashboard;
  },
});
</script>

<template>

  <el-row :gutter="14"><el-col :md="14" :xs="24"><el-card shadow="never" header="未解决质量问题"><el-table :data="qualityIssues" max-height="490"><el-table-column prop="severity" label="级别" width="85"><template #default="{ row }"><el-tag :type="row.severity === 'blocking' || row.severity === 'error' ? 'danger' : 'warning'">{{ row.severity }}</el-tag></template></el-table-column><el-table-column prop="capability" label="能力"/><el-table-column prop="symbol" label="标的"/><el-table-column prop="code" label="代码"/><el-table-column prop="message" label="说明" show-overflow-tooltip/></el-table></el-card></el-col><el-col :md="10" :xs="24"><el-card shadow="never" header="离线分钟导入"><el-alert :title="minuteDirectory || '离线目录未返回'" type="info" :closable="false" show-icon/><el-table :data="minuteImports" size="small" max-height="410" class="section-gap"><el-table-column prop="file_name" label="文件" show-overflow-tooltip/><el-table-column prop="status" label="状态" width="80"/><el-table-column prop="row_count" label="行数" width="90"/><el-table-column prop="rejected_rows" label="拒绝" width="70"/></el-table></el-card></el-col></el-row>

</template>

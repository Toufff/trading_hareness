<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue';

defineProps<{
  services: Record<string, any>;
  adapterHealth: Record<string, any>;
  runtimeHealth: Record<string, any>;
  loading: boolean;
  error: string;
  dateText: (value: any) => string;
  ageText: (value: any) => string;
  stateType: (value: any) => string;
  stateText: (value: any) => string;
  deliveryDetail: (value: any) => string | null | undefined;
}>();

defineEmits<{ refresh: [] }>();
</script>

<template>
  <el-card shadow="never" class="realtime-health-panel">
    <template #header>
      <div class="card-header">
        <div><span>盘中实时链路与日终摘要</span><small class="realtime-refresh-time">{{ services.session_active ? (services.special_window_active ? '连续竞价 · 特别关注窗口' : '连续竞价 · 常规窗口') : '休市/非连续竞价 · 服务待命；日终摘要独立按 19:15 窗口运行' }} · 更新于 {{ dateText(services.observed_at) }}</small></div>
        <el-space><el-tag :type="adapterHealth.status === 'ok' ? 'success' : 'danger'">适配器 {{ adapterHealth.status === 'ok' ? '正常' : '异常' }}</el-tag><el-tag :type="adapterHealth.quant_alert_configured ? 'success' : 'danger'">飞书 {{ adapterHealth.quant_alert_configured ? '已配置' : '未配置' }}</el-tag><el-button :icon="Refresh" :loading="loading" @click="$emit('refresh')">刷新</el-button></el-space>
      </div>
    </template>
    <el-alert v-if="error" :title="`实时状态读取失败：${error}`" type="error" :closable="false" show-icon/>
    <el-alert v-else-if="runtimeHealth.network?.state === 'offline'" :title="`外网暂时不可达：${runtimeHealth.network.last_error ?? '等待恢复'}。后台任务保持运行并在网络恢复后自动续跑，连续失败 ${runtimeHealth.network.consecutive_failures ?? 0} 次。`" type="error" :closable="false" show-icon/>
    <el-alert v-else-if="runtimeHealth.network?.state === 'degraded' || runtimeHealth.network?.state === 'recovering'" :title="`外部网络${runtimeHealth.network.state === 'recovering' ? '已恢复，正在续跑' : '不稳定'}：最近来源 ${runtimeHealth.network.last_source ?? '-'}；失败 ${runtimeHealth.network.consecutive_failures ?? 0} 次。`" type="warning" :closable="false" show-icon/>
    <el-alert v-else-if="services.summary?.decision_path_degraded" title="当前应运行的决策链路存在延迟或降级，请先检查对应数据源，系统不会把过期数据标记为健康。" type="error" :closable="false" show-icon/>
    <el-alert v-else :title="services.session_active ? `盘中链路正在按计划运行，观察池 ${services.summary?.enabled_watch_count ?? 0} 只。` : `当前为待命状态：${services.session_reason ?? '非交易时段'}。待命不等于故障。`" :type="services.session_active ? 'success' : 'info'" :closable="false" show-icon/>
    <el-alert v-if="runtimeHealth.daily_control_plane?.state === 'blocked'" class="section-gap" :title="`日线控制面待补：${runtimeHealth.daily_control_plane.trade_date ?? '最新交易日'} 的复权 ${runtimeHealth.daily_control_plane.adjustment_rows ?? 0}/${runtimeHealth.daily_control_plane.daily_rows ?? 0}、涨跌停 ${runtimeHealth.daily_control_plane.limit_rows ?? 0}/${runtimeHealth.daily_control_plane.daily_rows ?? 0}。该日期不进入依赖这些字段的策略判断。`" type="warning" :closable="false" show-icon/>
    <el-row :gutter="12" class="realtime-service-grid">
      <el-col v-for="service in services.items ?? []" :key="service.key" :xs="24" :sm="12" :lg="8">
        <el-card shadow="hover" class="realtime-service-card">
          <div class="realtime-service-head"><strong>{{ service.label }}</strong><el-tag :type="stateType(service.state)" effect="dark" size="small">{{ stateText(service.state) }}</el-tag></div>
          <p class="realtime-service-role">{{ service.role }}</p>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="运行窗口">{{ service.expected_active ? '当前应运行' : '当前不轮询' }}</el-descriptions-item>
            <el-descriptions-item label="频率">{{ service.cadence }}</el-descriptions-item>
            <el-descriptions-item label="最新数据">{{ dateText(service.last_observed_at) }}</el-descriptions-item>
            <el-descriptions-item label="数据年龄">{{ ageText(service.age_seconds) }}<span v-if="service.max_age_seconds"> / 门限 {{ ageText(service.max_age_seconds) }}</span></el-descriptions-item>
            <el-descriptions-item label="延迟/行数">{{ service.last_latency_ms ?? '-' }} ms / {{ service.last_row_count ?? '-' }}</el-descriptions-item>
            <el-descriptions-item v-if="deliveryDetail(service)" label="投递状态">{{ deliveryDetail(service) }}</el-descriptions-item>
          </el-descriptions>
          <el-tooltip v-if="service.last_error" :content="service.last_error" placement="top"><el-text class="realtime-service-error" type="danger" truncated>最近错误：{{ service.last_error }}</el-text></el-tooltip>
        </el-card>
      </el-col>
    </el-row>
  </el-card>
</template>

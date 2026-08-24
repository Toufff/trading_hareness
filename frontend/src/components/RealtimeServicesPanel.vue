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
  bytesText: (value: any) => string;
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
        <el-space><el-tag :type="services.edge_handoff?.state === 'ready' ? 'success' : services.edge_handoff?.configured ? 'warning' : 'info'">远端采集 {{ stateText(services.edge_handoff?.state ?? 'disabled') }}</el-tag><el-tag :type="adapterHealth.status === 'ok' ? 'success' : 'danger'">适配器 {{ adapterHealth.status === 'ok' ? '正常' : '异常' }}</el-tag><el-tag :type="adapterHealth.quant_alert_configured ? 'success' : 'danger'">飞书 {{ adapterHealth.quant_alert_configured ? '已配置' : '未配置' }}</el-tag><el-button :icon="Refresh" :loading="loading" @click="$emit('refresh')">刷新</el-button></el-space>
      </div>
    </template>
    <el-alert v-if="error" :title="`实时状态读取失败：${error}`" type="error" :closable="false" show-icon/>
    <el-alert v-else-if="runtimeHealth.network?.state === 'offline'" :title="`外网暂时不可达：${runtimeHealth.network.last_error ?? '等待恢复'}。后台任务保持运行并在网络恢复后自动续跑，连续失败 ${runtimeHealth.network.consecutive_failures ?? 0} 次。`" type="error" :closable="false" show-icon/>
    <el-alert v-else-if="runtimeHealth.network?.state === 'degraded' || runtimeHealth.network?.state === 'recovering'" :title="`外部网络${runtimeHealth.network.state === 'recovering' ? '已恢复，正在续跑' : '不稳定'}：最近来源 ${runtimeHealth.network.last_source ?? '-'}；失败 ${runtimeHealth.network.consecutive_failures ?? 0} 次。`" type="warning" :closable="false" show-icon/>
    <el-alert v-else-if="services.summary?.decision_path_degraded" title="当前应运行的决策链路存在延迟或降级，请先检查对应数据源，系统不会把过期数据标记为健康。" type="error" :closable="false" show-icon/>
    <el-alert v-else :title="services.session_active ? `盘中链路正在按计划运行，观察池 ${services.summary?.enabled_watch_count ?? 0} 只。` : `当前为待命状态：${services.session_reason ?? '非交易时段'}。待命不等于故障。`" :type="services.session_active ? 'success' : 'info'" :closable="false" show-icon/>
    <el-alert v-if="runtimeHealth.optional_background_tasks?.background_tasks_enabled === false" class="section-gap" title="此实例处于预热模式：仅验证 API 与依赖，不会获取任何采集或策略租约。" type="info" :closable="false" show-icon/>
    <el-alert v-if="services.edge_handoff?.configured" class="section-gap" :type="services.edge_handoff.state === 'ready' ? 'success' : 'warning'" :closable="false" show-icon :title="`远端采集节点 ${services.edge_handoff.state === 'ready' ? '正常' : '状态待刷新'}；证据最近同步 ${dateText(services.edge_handoff.last_imported_at)}（${ageText(services.edge_handoff.age_seconds)}前）。本地只做研究/回放，远端采集与本地分析状态分开显示。`"/>
    <el-alert v-if="services.edge_handoff?.pull?.last_error" class="section-gap" type="warning" :closable="false" show-icon :title="`远端证据拉取最近失败：${services.edge_handoff.pull.last_error}；最近尝试 ${dateText(services.edge_handoff.pull.last_attempt_at)}，最近成功 ${dateText(services.edge_handoff.pull.last_success_at)}。远端采集不会停止，但本地分析将等待下次成功同步。`"/>
    <el-alert v-if="services.edge_handoff?.runtime?.resources?.state === 'warning' || services.edge_handoff?.runtime?.resources?.state === 'degraded'" class="section-gap" :type="services.edge_handoff.runtime.resources.state === 'degraded' ? 'error' : 'warning'" :closable="false" show-icon :title="`远端采集节点磁盘${services.edge_handoff.runtime.resources.state === 'degraded' ? '低于采集保护下限' : '进入预警水位'}：可用 ${bytesText(services.edge_handoff.runtime.resources.disk_free_bytes)}；预警线 ${bytesText(services.edge_handoff.runtime.resources.disk_warning_free_bytes)}，保护下限 ${bytesText(services.edge_handoff.runtime.resources.disk_min_free_bytes)}。请释放空间；系统不会删除研究证据。`"/>
    <el-descriptions v-if="services.edge_handoff?.runtime?.build?.git_sha" class="section-gap" title="远端采集发布版本" :column="1" border size="small"><el-descriptions-item label="Git SHA">{{ services.edge_handoff.runtime.build.git_sha }}</el-descriptions-item><el-descriptions-item label="发布标签">{{ services.edge_handoff.runtime.build.release ?? '-' }}</el-descriptions-item><el-descriptions-item label="构建时间">{{ dateText(services.edge_handoff.runtime.build.build_created_at) }}</el-descriptions-item></el-descriptions>
    <el-alert v-if="runtimeHealth.daily_control_plane?.state === 'blocked'" class="section-gap" :title="`日线控制面待补：${runtimeHealth.daily_control_plane.trade_date ?? '最新交易日'} 的全 A 日线 ${runtimeHealth.daily_control_plane.daily_rows ?? 0}/${runtimeHealth.daily_control_plane.expected_daily_rows ?? runtimeHealth.daily_control_plane.daily_rows ?? 0}（覆盖率 ${((runtimeHealth.daily_control_plane.coverage_ratio ?? 0) * 100).toFixed(2)}%，门槛 ${runtimeHealth.daily_control_plane.minimum_required_rows ?? '-'}）；复权 ${runtimeHealth.daily_control_plane.adjustment_rows ?? 0}/${runtimeHealth.daily_control_plane.daily_rows ?? 0}、涨跌停 ${runtimeHealth.daily_control_plane.limit_rows ?? 0}/${runtimeHealth.daily_control_plane.daily_rows ?? 0}。该日期不进入依赖这些字段的策略判断。`" type="warning" :closable="false" show-icon/>
    <el-descriptions v-if="Object.keys(runtimeHealth.runtime_loops ?? {}).length" class="section-gap" title="后台循环租约心跳" :column="1" border size="small">
      <el-descriptions-item v-for="(loop, key) in runtimeHealth.runtime_loops" :key="key" :label="String(key)">
        <el-tag :type="stateType(loop.state)" size="small">{{ stateText(loop.state) }}</el-tag>
        <span class="realtime-refresh-time"> · 租约心跳 {{ loop.lease_heartbeat_at ? dateText(loop.lease_heartbeat_at) : '未持有' }}</span>
        <el-text v-if="loop.last_error" class="realtime-service-error" type="danger"> · {{ loop.last_error }}</el-text>
      </el-descriptions-item>
    </el-descriptions>
    <el-descriptions v-if="Object.keys(services.edge_handoff?.runtime?.runtime_loops ?? {}).length" class="section-gap" title="远端盘中采集循环" :column="1" border size="small">
      <el-descriptions-item v-for="(loop, key) in services.edge_handoff.runtime.runtime_loops" :key="key" :label="String(key)">
        <el-tag :type="stateType(loop.state)" size="small">{{ stateText(loop.state) }}</el-tag>
        <span class="realtime-refresh-time"> · 心跳 {{ loop.lease_heartbeat_at ? dateText(loop.lease_heartbeat_at) : '未持有' }}</span>
        <el-text v-if="loop.last_error" class="realtime-service-error" type="danger"> · {{ loop.last_error }}</el-text>
      </el-descriptions-item>
    </el-descriptions>
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

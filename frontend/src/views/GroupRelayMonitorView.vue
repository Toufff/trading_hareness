<script lang="ts">
import { defineComponent, inject } from 'vue';
import { dashboardContextKey } from '../dashboard-context';

export default defineComponent({
  name: 'GroupRelayMonitorView',
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('group relay monitor requires the dashboard shell context');
    return dashboard as Record<string, any>;
  },
});
</script>

<template>
  <el-card shadow="never" class="group-relay-status-panel">
    <template #header><div class="card-header"><div><span>群消息转发状态</span><small class="realtime-refresh-time">每 10 秒自动刷新；“最近源消息”与“最近成功轮询”分开显示。</small></div><el-space><el-button size="small" :icon="Refresh" :loading="groupRelayLoading" @click="loadGroupRelayStatus">刷新</el-button><el-button size="small" type="primary" @click="openCreateGroupRelayRoute">新增源群</el-button></el-space></div></template>
    <el-alert v-if="groupRelayError" :title="groupRelayError" type="error" :closable="false" show-icon />
    <template v-else>
      <el-descriptions :column="mobileLayout ? 1 : 5" border size="small">
        <el-descriptions-item label="整体状态"><el-tag :type="groupRelayStateType(groupRelayStatus.status)">{{ groupRelayStateText(groupRelayStatus.status) }}</el-tag></el-descriptions-item>
        <el-descriptions-item label="轮询间隔">{{ groupRelayStatus.interval_seconds ?? '-' }} 秒</el-descriptions-item>
        <el-descriptions-item label="用户读取授权"><el-tag :type="groupRelayStatus.user_oauth_configured ? oauthAuditTagType(groupRelayStatus.user_oauth_scope_audit) : 'danger'">{{ groupRelayStatus.user_oauth_configured ? oauthAuditLabel(groupRelayStatus.user_oauth_scope_audit) : '未配置' }}</el-tag></el-descriptions-item>
        <el-descriptions-item label="最近全局轮询">{{ dateText(groupRelayStatus.last_tick_completed_at) }}</el-descriptions-item>
        <el-descriptions-item label="写入端"><el-tag :type="groupRelayStateType(groupRelayStatus.writer?.state)">{{ groupRelayStateText(groupRelayStatus.writer?.state) }}</el-tag><div class="group-relay-age">{{ groupRelayStatus.writer?.owner_id ?? groupRelayStatus.writer?.configured_id ?? '未声明' }} · generation {{ groupRelayStatus.writer?.generation ?? '-' }}</div></el-descriptions-item>
        <el-descriptions-item label="远端投递队列"><el-tag :type="groupRelayStatus.delivery_outbox?.failed ? 'danger' : groupRelayStatus.delivery_outbox?.depth ? 'warning' : 'success'">待投递 {{ groupRelayStatus.delivery_outbox?.depth ?? 0 }} · 终止失败 {{ groupRelayStatus.delivery_outbox?.failed ?? 0 }}</el-tag><div v-if="groupRelayStatus.delivery_outbox?.paused" class="group-relay-age">按操作暂停 {{ groupRelayStatus.delivery_outbox.paused }}（不自动重试）</div></el-descriptions-item>
        <el-descriptions-item label="汇总群入站"><el-tag :type="groupRelayStateType(groupRelayStatus.summary_listener?.state)">{{ groupRelayStateText(groupRelayStatus.summary_listener?.state) }}</el-tag><div class="group-relay-age">本次运行新交 n8n {{ groupRelayStatus.summary_listener?.processed_count ?? 0 }} · 重复 {{ groupRelayStatus.summary_listener?.duplicate_count ?? 0 }} · 忽略 {{ groupRelayStatus.summary_listener?.ignored_count ?? 0 }}</div><div class="group-relay-age">最近消息 {{ dateText(groupRelayStatus.summary_listener?.last_source_message_at) }}</div><div v-if="groupRelayStatus.summary_listener?.last_error" class="group-relay-age">{{ groupRelayStatus.summary_listener.last_error }}</div></el-descriptions-item>
      </el-descriptions>
      <el-table :data="groupRelayStatus.sources ?? []" size="small" max-height="360" class="section-gap group-relay-table">
        <el-table-column prop="chat_name" label="源群" min-width="190" show-overflow-tooltip/>
        <el-table-column label="标签" width="110"><template #default="{ row }"><el-tag size="small" effect="plain">#{{ row.tag }}</el-tag></template></el-table-column>
        <el-table-column label="监听状态" width="118"><template #default="{ row }"><el-tag size="small" :type="groupRelayStateType(row.state)">{{ groupRelayStateText(row.state) }}</el-tag></template></el-table-column>
        <el-table-column label="最近成功轮询" min-width="155"><template #default="{ row }"><div>{{ dateText(row.last_polled_at) }}</div><small class="group-relay-age">{{ ageText(row.poll_age_seconds) }} 前</small></template></el-table-column>
        <el-table-column label="最近源消息" min-width="155"><template #default="{ row }"><div>{{ dateText(row.last_source_message_at) }}</div><small class="group-relay-age">{{ groupRelayMessageText(row.last_message_status) }}</small></template></el-table-column>
        <el-table-column label="投递验收" min-width="145"><template #default="{ row }"><el-tag size="small" :type="relayDeliveryTagType(row.delivery_state)">{{ relayDeliveryLabel(row.delivery_state) }}</el-tag><div v-if="row.last_forwarded_at" class="group-relay-age">{{ dateText(row.last_forwarded_at) }}</div></template></el-table-column>
        <el-table-column label="n8n / 远端" min-width="160"><template #default="{ row }"><el-tag size="small" :type="ingestionDeliveryTagType(row.ingestion)">{{ ingestionDeliveryLabel(row.ingestion) }}</el-tag><div v-if="row.ingestion?.last_updated_at" class="group-relay-age">{{ dateText(row.ingestion.last_updated_at) }}</div></template></el-table-column>
        <el-table-column label="编辑/撤回对账" min-width="155"><template #default="{ row }">{{ dateText(row.last_reconciled_at) }}</template></el-table-column>
        <el-table-column label="失败待重试" width="105"><template #default="{ row }"><el-tag size="small" :type="row.failed_count ? 'danger' : 'success'">{{ row.failed_count ?? 0 }}</el-tag></template></el-table-column>
        <el-table-column label="最近错误" min-width="190" show-overflow-tooltip><template #default="{ row }">{{ row.last_error || '-' }}</template></el-table-column>
        <el-table-column label="已恢复错误" min-width="190" show-overflow-tooltip><template #default="{ row }"><div>{{ row.last_resolved_error || '-' }}</div><small v-if="row.last_resolved_error_at" class="group-relay-age">{{ dateText(row.last_resolved_error_at) }}</small></template></el-table-column>
        <el-table-column label="管理" width="170" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openEditGroupRelayRoute(row)">编辑</el-button><el-button link :type="row.enabled === false ? 'success' : 'warning'" @click="setGroupRelayRouteEnabled(row, row.enabled === false)">{{ row.enabled === false ? '启用' : '停用' }}</el-button><el-button link type="danger" @click="deleteGroupRelayRoute(row)">删除</el-button></template></el-table-column>
      </el-table>
    </template>
  </el-card>
  <el-card shadow="never"><template #header><div class="card-header"><span>导入事件</span><el-select v-model="eventFilter" size="small" class="event-filter"><el-option label="全部状态" value="all"/><el-option label="已完成" value="已完成"/><el-option label="失败" value="失败"/><el-option label="处理中" value="已接收，处理中"/></el-select></div></template><el-empty v-if="!visibleEvents.length" description="暂无事件"/><el-timeline v-else><el-timeline-item v-for="event in visibleEvents" :key="event.event_id" :timestamp="dateText(event.received_at)" :type="event.n8n_status === '失败' ? 'danger' : 'primary'"><el-card shadow="never"><div class="event-title"><strong>{{ event.message_type || 'message' }}</strong><el-tag size="small">{{ event.n8n_status || '未知' }}</el-tag></div><p v-if="event.text">{{ event.text }}</p><el-text type="info">{{ event.source_label || '无来源备注' }}{{ event.n8n_error ? ` · ${event.n8n_error}` : '' }}</el-text></el-card></el-timeline-item></el-timeline></el-card>
</template>

import { ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';

import { groupRelayApi } from '../api/group-relay';
import { feishuWorkbenchApi } from '../api/feishu-workbench';

type OAuthScopeAudit = { required_scopes?: string[]; granted_scopes?: string[]; missing_scopes?: string[]; verified?: boolean | null };
type IngestionRouteStatus = { job_count?: number; completed_count?: number; failed_count?: number; paused_count?: number; state?: 'completed' | 'processing' | 'stalled' | 'failed' | 'filtered' | 'paused' | 'awaiting_message'; latest_status?: string | null; latest_stage?: string | null; remote_batch_id?: string | null; last_updated_at?: string | null; error_class?: string | null; error_message?: string | null; latest_failure_error?: string | null; latest_failure_at?: string | null };
type GroupRelaySourceStatus = { key: string; tag: string; chat_name: string; target_chat_ids?: string[]; enabled?: boolean; state: string; delivery_state?: 'verified' | 'awaiting_message' | 'failed'; last_polled_at?: string | null; poll_age_seconds?: number | null; last_source_message_at?: string | null; last_forwarded_at?: string | null; last_reconciled_at?: string | null; last_message_status?: string | null; failed_count?: number; ingestion?: IngestionRouteStatus | null; last_error?: string | null; last_resolved_error?: string | null; last_resolved_error_at?: string | null };
type SummaryListenerStatus = { enabled?: boolean; chat_configured?: boolean; state?: string; interval_seconds?: number; last_tick_completed_at?: string | null; last_success_at?: string | null; last_source_message_at?: string | null; poll_age_seconds?: number | null; processed_count?: number; duplicate_count?: number; ignored_count?: number; last_error?: string | null };
type RelayWriterStatus = { configured_id?: string | null; state?: string; owner_id?: string | null; generation?: number | null; updated_at?: string | null };
type DeliveryOutboxStatus = { depth?: number; failed?: number; paused?: number };
type GroupRelayStatus = { status?: string; observed_at?: string; enabled?: boolean; interval_seconds?: number; stale_after_seconds?: number; user_oauth_configured?: boolean; user_oauth_scope_audit?: OAuthScopeAudit | null; target_configured?: boolean; delivery_verified?: boolean; last_tick_started_at?: string | null; last_tick_completed_at?: string | null; last_tick_error?: string | null; writer?: RelayWriterStatus; delivery_outbox?: DeliveryOutboxStatus; sources?: GroupRelaySourceStatus[]; summary_listener?: SummaryListenerStatus };
type GroupRelayRouteForm = { key: string; chat_name: string; chat_id: string; tag: string; target_chat_ids_text: string; target_chat_names_text: string; enabled: boolean };
type FeishuCapability = { key: string; label: string; category: string; enabled: boolean; configured: boolean; resource_configured?: boolean; implementation_ready?: boolean; authorization_subject?: 'user' | 'tenant'; authorization_status?: 'verified' | 'missing' | 'unknown' | 'awaiting_verification' | 'not_required'; missing_user_scopes?: string[]; missing_tenant_scopes?: string[]; requires: string[]; note: string };
type FeishuEventSubscription = { event_type: string; label: string; required_for: string; handler_registered: boolean; state: 'received' | 'awaiting_callback'; received_count?: number; last_received_at?: string | null };
type FeishuApplicationInspection = { status?: 'not_checked' | 'verified' | 'missing_inspection_scope' | 'error'; checked_at?: string | null; scopes?: string[]; app_status?: number | null; target_chat?: { status?: 'not_checked' | 'not_configured' | 'verified' | 'error'; message?: string }; message?: string };
type FeishuWorkbenchStatus = { target_configured?: boolean; public_h5_url?: string | null; user_oauth_configured?: boolean; user_oauth_scopes?: string; user_oauth_scope_audit?: OAuthScopeAudit | null; application_inspection?: FeishuApplicationInspection; capabilities?: FeishuCapability[]; event_subscriptions?: FeishuEventSubscription[] };
type FeishuWorkbenchMessage = { source_message_id: string; route_tag: string; source_chat_name?: string | null; message?: { msg_type?: string; body?: { content?: string } }; status: string; workflow_state?: string; workflow_note?: string | null; source_deleted?: boolean; source_create_time?: number; forwarded_at?: string | null; updated_at?: string | null; action_card_message_id?: string | null };
type WorkbenchIntegrationKind = 'documents' | 'wiki-documents' | 'base-records' | 'calendar-events' | 'approvals';

const defaultRouteForm = (): GroupRelayRouteForm => ({ key: '', chat_name: '', chat_id: '', tag: '', target_chat_ids_text: '', target_chat_names_text: '', enabled: true });

export function useFeishuRelayWorkspace() {
  const groupRelayStatus = ref<GroupRelayStatus>({ sources: [] });
  const groupRelayLoading = ref(false);
  const groupRelayError = ref('');
  const groupRelayRouteDialog = ref(false);
  const groupRelayRouteSaving = ref(false);
  const groupRelayRouteForm = ref<GroupRelayRouteForm>(defaultRouteForm());
  const feishuWorkbench = ref<FeishuWorkbenchStatus>({ capabilities: [] });
  const feishuWorkbenchMessages = ref<FeishuWorkbenchMessage[]>([]);
  const feishuWorkbenchLoading = ref(false);
  const feishuWorkbenchError = ref('');
  const feishuWorkbenchAction = ref('');
  const workbenchSearch = ref('');
  const workbenchSearchResult = ref<unknown>(null);
  const workbenchIntegrationDialog = ref(false);
  const workbenchIntegration = ref({ kind: 'documents' as WorkbenchIntegrationKind, title: '新建飞书文档', payloadText: '{\n  "title": "群消息研究笔记"\n}' });

  const groupRelayStateType = (state?: string): 'success' | 'warning' | 'danger' | 'info' => state === 'healthy' || state === 'writer' ? 'success' : ['starting', 'delayed'].includes(state ?? '') ? 'warning' : ['error', 'unavailable', 'degraded', 'not_configured'].includes(state ?? '') ? 'danger' : 'info';
  const groupRelayStateText = (state?: string) => ({ healthy: '正常监听', writer: '当前写入端', fenced: '已围栏，仅观察', starting: '建立基线中', delayed: '轮询延迟', degraded: '有失败待重试', error: '读取异常', unavailable: '群不可读', not_configured: '授权未配置', disabled: '已停用' }[state ?? 'starting'] ?? state ?? '未知');
  const groupRelayMessageText = (status?: string | null) => ({ sent: '已转发', skipped_bootstrap: '历史基线', filtered_system: '系统消息已过滤', processing: '转发中', failed: '转发失败', unsupported: '不支持' }[status ?? ''] ?? '暂无消息');
  const oauthAuditLabel = (audit?: OAuthScopeAudit | null) => audit?.verified === true ? '权限已验证' : audit?.verified === false ? `缺少 ${audit.missing_scopes?.length ?? 0} 项权限` : '权限待验证';
  const oauthAuditTagType = (audit?: OAuthScopeAudit | null) => audit?.verified === true ? 'success' : audit?.verified === false ? 'danger' : 'warning';
  const relayDeliveryLabel = (state?: GroupRelaySourceStatus['delivery_state']) => ({ verified: '已实际转发', awaiting_message: '等待新消息验证', failed: '转发失败' }[state ?? 'awaiting_message'] ?? '等待新消息验证');
  const relayDeliveryTagType = (state?: GroupRelaySourceStatus['delivery_state']) => state === 'verified' ? 'success' : state === 'failed' ? 'danger' : 'warning';
  const ingestionDeliveryLabel = (ingestion?: IngestionRouteStatus | null) => {
    if (!ingestion?.latest_status) return '暂无汇总群入站';
    if (ingestion.state === 'filtered') return '系统消息已过滤';
    if (ingestion.state === 'paused') return '已按操作暂停';
    if (ingestion.state === 'completed') return ingestion.remote_batch_id ? '远端已入档' : '已提交远端';
    if (ingestion.state === 'stalled') return '下游状态卡住';
    if (ingestion.state === 'failed') return '远端待恢复';
    return 'n8n / 远端处理中';
  };
  const ingestionDeliveryTagType = (ingestion?: IngestionRouteStatus | null): 'success' | 'warning' | 'danger' | 'info' => ingestion?.state === 'completed' ? 'success' : ingestion?.state === 'stalled' || ingestion?.state === 'failed' ? 'danger' : ingestion?.state === 'processing' ? 'warning' : 'info';
  const applicationInspectionLabel = (inspection?: FeishuApplicationInspection) => ({ verified: '已读取', missing_inspection_scope: '缺少复核权限', error: '复核失败', not_checked: '未复核' }[inspection?.status ?? 'not_checked'] ?? '未复核');
  const applicationInspectionTagType = (inspection?: FeishuApplicationInspection) => inspection?.status === 'verified' ? 'success' : inspection?.status === 'missing_inspection_scope' || inspection?.status === 'error' ? 'danger' : 'warning';
  const targetChatInspectionLabel = (inspection?: FeishuApplicationInspection) => ({ verified: '应用可读取', error: '读取失败', not_configured: '未配置', not_checked: '未检查' }[inspection?.target_chat?.status ?? 'not_checked'] ?? '未检查');
  const targetChatInspectionTagType = (inspection?: FeishuApplicationInspection) => inspection?.target_chat?.status === 'verified' ? 'success' : inspection?.target_chat?.status === 'error' ? 'danger' : 'warning';
  const capabilityAuthorizationLabel = (item: FeishuCapability) => ({ verified: '用户权限已验证', missing: '用户权限缺失', unknown: '用户权限待验证', awaiting_verification: '待后台验收', not_required: '无需权限验收' }[item.authorization_status ?? 'awaiting_verification'] ?? '待后台验收');
  const capabilityAuthorizationTagType = (item: FeishuCapability) => item.authorization_status === 'verified' || item.authorization_status === 'not_required' ? 'success' : item.authorization_status === 'missing' ? 'danger' : 'warning';

  async function loadGroupRelayStatus() {
    groupRelayLoading.value = true; groupRelayError.value = '';
    try { groupRelayStatus.value = await groupRelayApi.status<GroupRelayStatus>(); }
    catch (error) { groupRelayError.value = error instanceof Error ? error.message : String(error); }
    finally { groupRelayLoading.value = false; }
  }
  async function loadFeishuWorkbench() {
    feishuWorkbenchLoading.value = true; feishuWorkbenchError.value = '';
    try {
      const [status, messages] = await Promise.all([feishuWorkbenchApi.status<FeishuWorkbenchStatus>(), feishuWorkbenchApi.messages<{ items?: FeishuWorkbenchMessage[] }>()]);
      feishuWorkbench.value = status; feishuWorkbenchMessages.value = messages.items ?? [];
    } catch (error) { feishuWorkbenchError.value = error instanceof Error ? error.message : String(error); }
    finally { feishuWorkbenchLoading.value = false; }
  }
  async function inspectFeishuApplication() {
    feishuWorkbenchAction.value = 'application-inspection';
    try { const result = await feishuWorkbenchApi.inspectApplication<{ inspection?: FeishuApplicationInspection }>(); ElMessage.info(result.inspection?.message ?? '已完成飞书后台配置复核'); await loadFeishuWorkbench(); }
    catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
    finally { feishuWorkbenchAction.value = ''; }
  }
  function workbenchMessageText(item: FeishuWorkbenchMessage) {
    try { const content = item.message?.body?.content ? JSON.parse(item.message.body.content) : {}; return String(content.text ?? content.title ?? `[${item.message?.msg_type ?? '消息'}]`).slice(0, 160); }
    catch { return `[${item.message?.msg_type ?? '消息'}]`; }
  }
  const workbenchWorkflowText = (state?: string) => ({ new: '待处理', research: '纳入研究', focus: '重点关注', task: '已建任务', ignored: '已忽略', recalled: '已撤回' }[state ?? 'new'] ?? state ?? '待处理');
  async function runWorkbenchAction(item: FeishuWorkbenchMessage, action: string) {
    feishuWorkbenchAction.value = `${item.source_message_id}:${action}`;
    try { await feishuWorkbenchApi.updateMessageState(item.source_message_id, action); ElMessage.success('协作状态已更新'); await loadFeishuWorkbench(); }
    catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
    finally { feishuWorkbenchAction.value = ''; }
  }
  async function searchFeishuMessages() {
    if (!workbenchSearch.value.trim()) return;
    feishuWorkbenchAction.value = 'search';
    try { workbenchSearchResult.value = await feishuWorkbenchApi.searchMessages<Record<string, unknown>>(workbenchSearch.value.trim()); }
    catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
    finally { feishuWorkbenchAction.value = ''; }
  }
  function openWorkbenchIntegration(kind: WorkbenchIntegrationKind) {
    const presets: Record<WorkbenchIntegrationKind, { title: string; payloadText: string }> = {
      documents: { title: '新建飞书文档', payloadText: '{\n  "title": "群消息研究笔记",\n  "contentType": "markdown",\n  "content": "# 群消息研究笔记\\n\\n请在此补充结论。"\n}' },
      'wiki-documents': { title: '新建并归档到 Wiki', payloadText: '{\n  "title": "群消息研究笔记",\n  "contentType": "markdown",\n  "content": "# 群消息研究笔记\\n\\n请在此补充结论。"\n}' },
      'base-records': { title: '写入 Base 研究台账', payloadText: '{\n  "fields": {\n    "标题": "群消息跟进",\n    "状态": "待处理"\n  }\n}' },
      'calendar-events': { title: '创建日历提醒', payloadText: `{\n  "summary": "群消息跟进",\n  "description": "",\n  "startTime": "${new Date(Date.now() + 3600_000).toISOString()}",\n  "endTime": "${new Date(Date.now() + 7200_000).toISOString()}"\n}` },
      approvals: { title: '发起审批', payloadText: '{\n  "form": {}\n}' },
    };
    workbenchIntegration.value = { kind, ...presets[kind] }; workbenchIntegrationDialog.value = true;
  }
  async function runWorkbenchEndpoint(path: string, key: string, body: Record<string, unknown> = {}) {
    feishuWorkbenchAction.value = key;
    try { await feishuWorkbenchApi.submit(path, body); ElMessage.success('已提交到飞书'); await loadFeishuWorkbench(); }
    catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
    finally { feishuWorkbenchAction.value = ''; }
  }
  async function createWorkbenchDigest() {
    try { await ElMessageBox.confirm('将把近期未忽略的汇总消息合并为一条摘要发送到汇总群。', '生成群摘要', { type: 'warning', confirmButtonText: '生成并发送', cancelButtonText: '取消' }); }
    catch { return; }
    await runWorkbenchEndpoint('/api/feishu-workbench/digests', 'digest', { limit: 12 });
  }
  async function createWorkbenchTab() {
    try { await ElMessageBox.confirm('将为汇总群创建一个指向公网 H5 工作台的群 Tab。', '创建汇总群 Tab', { type: 'warning', confirmButtonText: '创建', cancelButtonText: '取消' }); }
    catch { return; }
    await runWorkbenchEndpoint('/api/feishu-workbench/group-tabs', 'group-tab');
  }
  async function submitWorkbenchIntegration() {
    let payload: Record<string, unknown>;
    try { payload = JSON.parse(workbenchIntegration.value.payloadText) as Record<string, unknown>; } catch { ElMessage.error('请输入有效 JSON'); return; }
    feishuWorkbenchAction.value = `integration:${workbenchIntegration.value.kind}`;
    try { await feishuWorkbenchApi.submit(`/api/feishu-workbench/${workbenchIntegration.value.kind}`, payload); ElMessage.success('已提交到飞书'); workbenchIntegrationDialog.value = false; }
    catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
    finally { feishuWorkbenchAction.value = ''; }
  }
  function openCreateGroupRelayRoute() { groupRelayRouteForm.value = defaultRouteForm(); groupRelayRouteDialog.value = true; }
  function openEditGroupRelayRoute(route: GroupRelaySourceStatus) { groupRelayRouteForm.value = { key: route.key, chat_name: route.chat_name, chat_id: '', tag: route.tag, target_chat_ids_text: (route.target_chat_ids ?? []).join(', '), target_chat_names_text: '', enabled: route.enabled !== false }; groupRelayRouteDialog.value = true; }
  async function saveGroupRelayRoute() {
    const form = groupRelayRouteForm.value; groupRelayRouteSaving.value = true;
    try {
      await groupRelayApi.upsertRoute<{ route: GroupRelaySourceStatus }>(form.key, { chat_name: form.chat_name, chat_id: form.chat_id || undefined, tag: form.tag, target_chat_ids: form.target_chat_ids_text.split(',').map((value) => value.trim()).filter(Boolean), target_chat_names: form.target_chat_names_text.split(',').map((value) => value.trim()).filter(Boolean), enabled: form.enabled });
      groupRelayRouteDialog.value = false; ElMessage.success(form.key ? '源群配置已更新，将在下一次轮询生效' : '源群已注册，将在下一次轮询建立基线'); await loadGroupRelayStatus();
    } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
    finally { groupRelayRouteSaving.value = false; }
  }
  async function setGroupRelayRouteEnabled(route: GroupRelaySourceStatus, enabled: boolean) {
    try { await groupRelayApi.upsertRoute(route.key, { chat_name: route.chat_name, tag: route.tag, enabled }); ElMessage.success(enabled ? '已启用，下一次轮询生效' : '已停用该源群'); await loadGroupRelayStatus(); }
    catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)); }
  }
  async function deleteGroupRelayRoute(route: GroupRelaySourceStatus) {
    try { await ElMessageBox.confirm(`确认停止监听“${route.chat_name}”并删除其注册信息？已有转发记录会保留。`, '删除源群', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }); await groupRelayApi.removeRoute<Record<string, unknown>>(route.key); ElMessage.success('源群已删除，下一次轮询起停止监听'); await loadGroupRelayStatus(); }
    catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : String(error)); }
  }

  return {
    groupRelayStatus, groupRelayLoading, groupRelayError, groupRelayRouteDialog, groupRelayRouteSaving, groupRelayRouteForm,
    feishuWorkbench, feishuWorkbenchMessages, feishuWorkbenchLoading, feishuWorkbenchError, feishuWorkbenchAction, workbenchSearch, workbenchSearchResult, workbenchIntegrationDialog, workbenchIntegration,
    groupRelayStateType, groupRelayStateText, groupRelayMessageText, oauthAuditLabel, oauthAuditTagType, relayDeliveryLabel, relayDeliveryTagType, ingestionDeliveryLabel, ingestionDeliveryTagType, applicationInspectionLabel, applicationInspectionTagType, targetChatInspectionLabel, targetChatInspectionTagType, capabilityAuthorizationLabel, capabilityAuthorizationTagType,
    loadGroupRelayStatus, loadFeishuWorkbench, inspectFeishuApplication, workbenchMessageText, workbenchWorkflowText, runWorkbenchAction, searchFeishuMessages, openWorkbenchIntegration, runWorkbenchEndpoint, createWorkbenchDigest, createWorkbenchTab, submitWorkbenchIntegration, openCreateGroupRelayRoute, openEditGroupRelayRoute, saveGroupRelayRoute, setGroupRelayRouteEnabled, deleteGroupRelayRoute,
  };
}

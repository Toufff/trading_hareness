<script setup lang="ts">
import { defineAsyncComponent } from 'vue';
import { DataAnalysis, Document, Operation, Refresh, UploadFilled, Wallet } from '@element-plus/icons-vue';
import ManualRelayView from './views/ManualRelayView.vue';
import GroupRelayMonitorView from './views/GroupRelayMonitorView.vue';
import FeishuWorkbenchView from './views/FeishuWorkbenchView.vue';
import { useDashboardWorkspace } from './composables/useDashboardWorkspace';

const dashboard = useDashboardWorkspace();
const ResearchOverviewTab = defineAsyncComponent(() => import('./views/research/ResearchOverviewTab.vue'));
const MarketSnapshotsTab = defineAsyncComponent(() => import('./views/research/MarketSnapshotsTab.vue'));
const CloseReviewTab = defineAsyncComponent(() => import('./views/research/CloseReviewTab.vue'));
const StrategyTab = defineAsyncComponent(() => import('./views/research/StrategyTab.vue'));
const FactorLabTab = defineAsyncComponent(() => import('./views/research/FactorLabTab.vue'));
const StockStudyTab = defineAsyncComponent(() => import('./views/research/StockStudyTab.vue'));
const AnalystEvidenceTab = defineAsyncComponent(() => import('./views/research/AnalystEvidenceTab.vue'));
const ClaimReviewTab = defineAsyncComponent(() => import('./views/research/ClaimReviewTab.vue'));
const ProviderTab = defineAsyncComponent(() => import('./views/research/ProviderTab.vue'));
const CatalogTab = defineAsyncComponent(() => import('./views/research/CatalogTab.vue'));
const QualityTab = defineAsyncComponent(() => import('./views/research/QualityTab.vue'));
const PersonalDecisionView = defineAsyncComponent(() => import('./views/PersonalDecisionView.vue'));
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="236px" class="side-nav">
      <div class="brand"><el-icon><DataAnalysis /></el-icon><div><strong>Quant Research</strong><span>投研与市场数据</span></div></div>
      <el-menu :default-active="dashboard.activeSection" class="menu" @select="dashboard.selectActiveSection">
        <el-menu-item index="research"><el-icon><DataAnalysis /></el-icon><span>量化研究台</span></el-menu-item>
        <el-menu-item index="personal"><el-icon><Wallet /></el-icon><span>个人决策</span></el-menu-item>
        <el-menu-item index="monitor"><el-icon><Operation /></el-icon><span>导入监控</span></el-menu-item>
        <el-menu-item index="workbench"><el-icon><Document /></el-icon><span>飞书工作台</span></el-menu-item>
        <el-menu-item index="relay"><el-icon><UploadFilled /></el-icon><span>手动投递</span></el-menu-item>
      </el-menu>
      <div class="side-state"><el-tag :type="dashboard.connected ? 'success' : 'warning'" effect="plain">{{ dashboard.connected ? '事件流已连接' : '事件流重连中' }}</el-tag></div>
    </el-aside>
    <el-container>
      <el-header class="topbar"><div><h1>{{ dashboard.activeSection === 'research' ? '量化研究台' : dashboard.activeSection === 'personal' ? '个人决策' : dashboard.activeSection === 'monitor' ? '导入监控' : dashboard.activeSection === 'workbench' ? '飞书工作台' : '手动投递' }}</h1><span>{{ dashboard.activeSection === 'research' ? '分析师证据、市场数据与研究候选池' : dashboard.activeSection === 'personal' ? '实际持仓、市场判断与可执行的新买计划' : dashboard.activeSection === 'workbench' ? '汇总群协作闭环、可用能力与授权状态' : '本地持久化导入链路' }}</span></div><el-button v-if="dashboard.activeSection !== 'personal'" :icon="Refresh" :loading="dashboard.activeSection === 'workbench' ? dashboard.feishuWorkbenchLoading : dashboard.loading" @click="dashboard.activeSection === 'workbench' ? dashboard.loadFeishuWorkbench() : dashboard.loadResearch()">刷新数据</el-button></el-header>
      <el-main class="content">
        <template v-if="dashboard.activeSection === 'research'">
          <el-alert v-if="dashboard.researchError" :title="dashboard.researchError" type="error" show-icon :closable="false" class="section-gap" />
          <el-tabs v-model="dashboard.activeResearchTab" class="research-tabs">
            <el-tab-pane label="研究概览" name="overview">
              <ResearchOverviewTab />
            </el-tab-pane>
            <el-tab-pane label="全市场快照" name="market-snapshots">
              <MarketSnapshotsTab />
            </el-tab-pane>
            <el-tab-pane label="收盘复盘" name="close-review">
              <CloseReviewTab />
            </el-tab-pane>
            <el-tab-pane label="策略与股票池" name="strategy">
              <StrategyTab />
            </el-tab-pane>
            <el-tab-pane label="因子与回测" name="factor-lab">
              <FactorLabTab />
            </el-tab-pane>
            <el-tab-pane label="个股研究" name="stock-study">
              <StockStudyTab />
            </el-tab-pane>
            <el-tab-pane label="分析师证据" name="evidence">
              <AnalystEvidenceTab />
            </el-tab-pane>
            <el-tab-pane label="观点复核" name="claim-review">
              <ClaimReviewTab />
            </el-tab-pane>
            <el-tab-pane label="数据源 Doctor" name="providers">
              <ProviderTab />
            </el-tab-pane>
            <el-tab-pane label="接口与原始数据" name="catalog">
              <CatalogTab />
            </el-tab-pane>
            <el-tab-pane label="质量与分钟数据" name="quality">
              <QualityTab />
            </el-tab-pane>
          </el-tabs>
        </template>
        <PersonalDecisionView v-else-if="dashboard.activeSection === 'personal'" />
        <GroupRelayMonitorView v-else-if="dashboard.activeSection === 'monitor'" />
        <FeishuWorkbenchView v-else-if="dashboard.activeSection === 'workbench'" />
        <ManualRelayView v-else />
      </el-main>
    </el-container>
  </el-container>
  <el-dialog v-model="dashboard.fetchDialogOpen" title="受控数据读取" width="680px" destroy-on-close><el-form label-position="top"><el-row :gutter="14"><el-col :span="12"><el-form-item label="API"><el-input v-model="dashboard.fetchForm.api_name"/></el-form-item></el-col><el-col :span="12"><el-form-item label="来源"><el-select v-model="dashboard.fetchForm.provider" class="full-width"><el-option label="自动回退" value="auto"/><el-option label="主 Tushare 源" value="primary"/><el-option label="Super 聚合兼容路由" value="super"/><el-option label="Super SDK 完整路径" value="super_sdk"/><el-option label="Super GET 已验证路径" value="super_get"/><el-option label="REST 备用源" value="backup"/></el-select></el-form-item></el-col></el-row><el-alert title="完整 ths_member 请选自动、Super 聚合或 Super SDK；Super GET 对大板块会被上游截断。" type="warning" :closable="false" class="section-gap"/><el-form-item label="参数 JSON"><el-input v-model="dashboard.fetchForm.paramsText" type="textarea" :rows="8" class="mono"/></el-form-item><el-row :gutter="14"><el-col :span="16"><el-form-item label="字段"><el-input v-model="dashboard.fetchForm.fields"/></el-form-item></el-col><el-col :span="8"><el-form-item label="最大行数"><el-input-number v-model="dashboard.fetchForm.max_rows" :min="1" :max="10000" class="full-width"/></el-form-item></el-col></el-row></el-form><template #footer><el-button @click="dashboard.fetchDialogOpen = false">取消</el-button><el-button type="primary" :loading="dashboard.actionLoading === 'fetch'" @click="dashboard.executeFetch">读取并保存证据</el-button></template></el-dialog>
  <el-dialog v-model="dashboard.groupRelayRouteDialog" :title="dashboard.groupRelayRouteForm.key ? '编辑源群' : '新增源群'" width="520px" destroy-on-close>
    <el-form label-position="top" @submit.prevent="dashboard.saveGroupRelayRoute">
	      <el-alert title="保存时会用用户读取权限搜索群名；若企业限制搜索，可填已知 chat_id 直接注册。新增群首次只建立历史基线，之后才转发新消息。" type="info" :closable="false" show-icon class="section-gap"/>
      <el-form-item label="源群名称" required><el-input v-model="dashboard.groupRelayRouteForm.chat_name" maxlength="120" placeholder="例如：新野人哥会员群【禁言】"/></el-form-item>
      <el-form-item label="转发标签" required><el-input v-model="dashboard.groupRelayRouteForm.tag" maxlength="32" placeholder="例如：quanneng"><template #prepend>#</template></el-input></el-form-item>
      <el-form-item label="群 chat_id（可选，仅同名群时需要）"><el-input v-model="dashboard.groupRelayRouteForm.chat_id" placeholder="oc_xxx"/></el-form-item>
      <el-form-item label="额外转发目标群名（推荐）"><el-input v-model="dashboard.groupRelayRouteForm.target_chat_names_text" placeholder="例如：anqiang分享群1, 复盘群"/><div class="form-help">填写飞书群的完整名称，保存时会用用户 OAuth 精确匹配并自动转换为 chat_id；同名群需要改填下方 ID。</div></el-form-item>
      <el-form-item label="已知目标群 chat_id（可选）"><el-input v-model="dashboard.groupRelayRouteForm.target_chat_ids_text" placeholder="oc_xxx, oc_yyy"/><div class="form-help">名称和 ID 可以同时填写，最多 8 个目标群；源消息仍会发送到主汇总群。请先在每个目标群中邀请机器人。</div></el-form-item>
      <el-form-item label="状态"><el-switch v-model="dashboard.groupRelayRouteForm.enabled" active-text="启用监听" inactive-text="停用监听"/></el-form-item>
    </el-form>
    <template #footer><el-button @click="dashboard.groupRelayRouteDialog = false">取消</el-button><el-button type="primary" :loading="dashboard.groupRelayRouteSaving" @click="dashboard.saveGroupRelayRoute">保存</el-button></template>
  </el-dialog>
  <el-dialog v-model="dashboard.workbenchIntegrationDialog" :title="dashboard.workbenchIntegration.title" width="620px" destroy-on-close>
    <el-alert title="此操作会真实调用飞书 API；仅在上方能力状态显示“可配置”且飞书后台权限已发布后提交。" type="warning" :closable="false" show-icon class="section-gap"/>
    <el-form label-position="top"><el-form-item label="请求 JSON"><el-input v-model="dashboard.workbenchIntegration.payloadText" type="textarea" :rows="12" class="mono"/></el-form-item></el-form>
    <template #footer><el-button @click="dashboard.workbenchIntegrationDialog = false">取消</el-button><el-button type="primary" :loading="dashboard.feishuWorkbenchAction.startsWith('integration:')" @click="dashboard.submitWorkbenchIntegration">提交</el-button></template>
  </el-dialog>
  <el-dialog v-model="dashboard.fetchResultOpen" title="读取结果" width="620px"><el-descriptions :column="2" border><el-descriptions-item v-for="(value, key) in dashboard.fetchResult" :key="String(key)" :label="String(key)"><span class="result-value">{{ typeof value === 'object' ? JSON.stringify(value) : value }}</span></el-descriptions-item></el-descriptions></el-dialog>
</template>

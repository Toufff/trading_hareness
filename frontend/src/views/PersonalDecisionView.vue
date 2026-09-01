<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue';
import { reactive } from 'vue';
import { usePersonalDecisionWorkspace, type TradePlan } from '../composables/usePersonalDecisionWorkspace';

const workspace = reactive(usePersonalDecisionWorkspace());

function actionLabel(action: TradePlan['action']): string {
  return ({
    hold: '继续持有', observe: '观察', buy_on_trigger: '条件买入',
    reduce_on_trigger: '条件减仓', exit_on_trigger: '条件退出', avoid: '回避',
  })[action];
}

function actionType(action: TradePlan['action']): 'success' | 'warning' | 'danger' | 'info' {
  if (action === 'buy_on_trigger') return 'success';
  if (action === 'reduce_on_trigger' || action === 'observe') return 'warning';
  if (action === 'exit_on_trigger' || action === 'avoid') return 'danger';
  return 'info';
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.join('、');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
</script>

<template>
  <section class="personal-decision">
    <el-card shadow="never" class="decision-toolbar">
      <div class="toolbar-row">
        <div>
          <h2>个人决策简报</h2>
          <p>盘面、实际持仓和新买计划独立生成；任何一段失败不会清空其他有效结论。</p>
        </div>
        <el-space>
          <el-input v-model="workspace.accountKey" aria-label="账户标识" class="account-input" @keyup.enter="workspace.load" />
          <el-button type="primary" :icon="Refresh" :loading="workspace.loading" @click="workspace.load">刷新</el-button>
        </el-space>
      </div>
    </el-card>

    <el-alert v-if="workspace.error" :title="workspace.error" type="error" show-icon :closable="false" class="section-gap" />
    <el-skeleton v-if="workspace.loading && !workspace.brief" :rows="8" animated class="section-gap" />

    <template v-if="workspace.brief">
      <div class="status-grid section-gap">
        <div class="status-tile"><span>整张简报</span><el-tag :type="workspace.brief.status === 'ready' ? 'success' : 'warning'">{{ workspace.brief.status === 'ready' ? '完整' : '部分可用' }}</el-tag></div>
        <div class="status-tile"><span>盘面分析</span><el-tag :type="workspace.brief.delivery.market_eligible ? 'success' : 'danger'">{{ workspace.brief.delivery.market_eligible ? '可用' : '缺失' }}</el-tag></div>
        <div class="status-tile"><span>持仓操作</span><el-tag :type="workspace.brief.delivery.holding_actions_eligible ? 'success' : 'danger'">{{ workspace.brief.delivery.holding_actions_eligible ? '可用' : '阻断' }}</el-tag></div>
        <div class="status-tile"><span>新买计划</span><el-tag :type="workspace.brief.delivery.new_buy_actions_eligible ? 'success' : 'info'">{{ workspace.brief.delivery.new_buy_actions_eligible ? '有计划' : '无合格计划' }}</el-tag></div>
      </div>

      <el-card shadow="never" class="section-gap decision-section">
        <template #header><div class="section-title"><div><strong>市场与板块</strong><small>{{ displayValue(workspace.marketContent.observed_at || workspace.brief.as_of_at) }}</small></div><el-tag effect="plain">{{ displayValue(workspace.marketContent.market_state) }}</el-tag></div></template>
        <el-empty v-if="!workspace.brief.delivery.market_eligible" description="没有可用的市场分析；这不会阻止已完成的新买计划显示" :image-size="52" />
        <el-descriptions v-else :column="2" border size="small">
          <el-descriptions-item label="交易日">{{ displayValue(workspace.marketContent.exchange_date) }}</el-descriptions-item>
          <el-descriptions-item label="阶段">{{ displayValue(workspace.marketContent.session) }}</el-descriptions-item>
          <el-descriptions-item v-for="(value, key) in workspace.marketReport" :key="String(key)" :label="String(key)" :span="2">{{ displayValue(value) }}</el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card shadow="never" class="section-gap decision-section">
        <template #header><div class="section-title"><div><strong>实际持仓操作</strong><small>持仓快照 {{ displayValue(workspace.brief.holdings.portfolio_observed_at) }}</small></div><span>{{ workspace.brief.holdings.actions?.length ?? 0 }} 项</span></div></template>
        <el-alert v-if="!workspace.brief.delivery.holding_actions_eligible" title="没有同时满足“精确持仓快照 + 完整交易计划”的持仓动作，系统不会用旧持仓或纸面账户代替。" type="warning" :closable="false" show-icon />
        <div v-for="item in workspace.brief.holdings.actions" :key="item.plan.plan_key" class="action-card">
          <div class="action-heading"><div><strong>{{ item.position.name }}</strong><span>（{{ item.position.symbol }}）</span></div><el-tag :type="actionType(item.plan.action)">{{ actionLabel(item.plan.action) }}</el-tag></div>
          <el-descriptions :column="4" size="small" border>
            <el-descriptions-item label="数量">{{ displayValue(item.position.quantity) }}</el-descriptions-item>
            <el-descriptions-item label="可卖">{{ displayValue(item.position.sellable_quantity) }}</el-descriptions-item>
            <el-descriptions-item label="成本">{{ displayValue(item.position.average_cost) }}</el-descriptions-item>
            <el-descriptions-item label="现价">{{ displayValue(item.position.market_price) }}</el-descriptions-item>
            <el-descriptions-item label="减仓条件" :span="2">{{ displayValue(item.plan.reduce_trigger) }}</el-descriptions-item>
            <el-descriptions-item label="退出条件" :span="2">{{ item.plan.exit_trigger }}</el-descriptions-item>
            <el-descriptions-item label="止损参考">{{ displayValue(item.plan.stop_price) }}</el-descriptions-item>
            <el-descriptions-item label="目标参考">{{ displayValue(item.plan.target_prices) }}</el-descriptions-item>
            <el-descriptions-item label="仓位上限">{{ item.plan.max_position_pct }}%</el-descriptions-item>
            <el-descriptions-item label="有效期">{{ item.plan.valid_until }}</el-descriptions-item>
          </el-descriptions>
          <ul class="rationale"><li v-for="reason in item.plan.rationale" :key="reason">{{ reason }}</li></ul>
        </div>
      </el-card>

      <el-card shadow="never" class="section-gap decision-section">
        <template #header><div class="section-title"><div><strong>新买机会</strong><small>只显示已经形成完整进出场计划的标的</small></div><span>{{ workspace.brief.new_buys.actions?.length ?? 0 }} 项</span></div></template>
        <el-empty v-if="!workspace.brief.new_buys.actions?.length" description="当前没有满足完整研究与交易计划要求的新买标的" :image-size="52" />
        <div v-for="plan in workspace.brief.new_buys.actions" :key="plan.plan_key" class="action-card buy-card">
          <div class="action-heading"><div><strong>{{ plan.name }}</strong><span>（{{ plan.symbol }}）</span></div><el-tag type="success">条件买入</el-tag></div>
          <el-descriptions :column="4" size="small" border>
            <el-descriptions-item label="买入区间">{{ displayValue(plan.entry_zone?.lower) }}–{{ displayValue(plan.entry_zone?.upper) }}</el-descriptions-item>
            <el-descriptions-item label="止损参考">{{ displayValue(plan.stop_price) }}</el-descriptions-item>
            <el-descriptions-item label="目标参考">{{ displayValue(plan.target_prices) }}</el-descriptions-item>
            <el-descriptions-item label="最大仓位">{{ plan.max_position_pct }}%</el-descriptions-item>
            <el-descriptions-item label="加仓条件" :span="2">{{ displayValue(plan.add_trigger) }}</el-descriptions-item>
            <el-descriptions-item label="失效条件" :span="2">{{ plan.exit_trigger }}</el-descriptions-item>
          </el-descriptions>
          <ul class="rationale"><li v-for="reason in plan.rationale" :key="reason">{{ reason }}</li></ul>
        </div>
      </el-card>

      <el-collapse v-if="workspace.brief.diagnostics?.length" class="section-gap diagnostics">
        <el-collapse-item title="内部诊断" name="diagnostics"><el-tag v-for="item in workspace.brief.diagnostics" :key="item" type="warning" effect="plain" class="diagnostic-tag">{{ item }}</el-tag></el-collapse-item>
      </el-collapse>
    </template>
  </section>
</template>

<style scoped>
.personal-decision { max-width: 1440px; margin: 0 auto; }
.decision-toolbar h2 { margin: 0 0 5px; font-size: 20px; }
.decision-toolbar p { margin: 0; color: var(--el-text-color-secondary); }
.toolbar-row, .section-title, .action-heading { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.account-input { width: 190px; }
.status-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.status-tile { display: flex; align-items: center; justify-content: space-between; padding: 15px 16px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: var(--el-bg-color); }
.section-title > div { display: flex; flex-direction: column; gap: 3px; }
.section-title small { color: var(--el-text-color-secondary); font-weight: 400; }
.action-card { padding: 15px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: var(--el-fill-color-blank); }
.action-card + .action-card { margin-top: 12px; }
.buy-card { border-left: 3px solid var(--el-color-success); }
.action-heading { margin-bottom: 12px; }
.action-heading strong { font-size: 17px; }
.action-heading span { color: var(--el-text-color-secondary); }
.rationale { margin: 12px 0 0; padding-left: 20px; color: var(--el-text-color-regular); }
.rationale li + li { margin-top: 5px; }
.diagnostic-tag { margin: 0 8px 8px 0; }
@media (max-width: 900px) {
  .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .toolbar-row { align-items: flex-start; flex-direction: column; }
}
</style>

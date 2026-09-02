<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue';
import { computed, reactive } from 'vue';
import { usePersonalDecisionWorkspace, type DecisionResearchGate, type TradePlan } from '../composables/usePersonalDecisionWorkspace';
import ResearchOnlyBadge from '../components/ResearchOnlyBadge.vue';

const workspace = reactive(usePersonalDecisionWorkspace());

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asTextList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function numberText(value: unknown, digits = 1): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : '—';
}

function stateLabel(value: unknown): string {
  return ({
    rotation_defensive: '防御板块占优', rotation_technology: '科技板块占优',
    broad_risk_on: '全市场风险偏好上升', broad_risk_off: '全市场风险偏好下降',
    mixed_or_neutral: '板块轮动、方向混合',
    insufficient_index_history: '指数历史不足，不能判定',
    corrective_rebound: '多指数纠错反弹', trend_recovery: '多指数趋势修复',
    weak_or_declining: '多指数偏弱', mixed_transition: '多指数过渡分化',
  } as Record<string, string>)[String(value)] ?? String(value || '尚未判定');
}

function qualityLabel(value: string): string {
  return ({
    multi_index_close_context_not_current: '多指数收盘背景并非当前交易日',
    missing_index_context: '缺少可用指数背景',
    missing_usable_breadth_snapshot: '缺少可用的全市场涨跌广度',
  } as Record<string, string>)[value] ?? value;
}

const marketMetrics = computed(() => asRecord(workspace.marketReport.market_state_metrics));
const indexContext = computed(() => asRecord(workspace.marketReport.index_breadth_context));
const multiIndex = computed(() => asRecord(indexContext.value.multi_index_regime));
const marketQualityFlags = computed(() => {
  const top = asTextList(workspace.marketContent.quality_flags);
  return top.length ? top : asTextList(indexContext.value.quality_flags);
});
const positiveFlowPct = computed(() => {
  const value = Number(marketMetrics.value.positive_flow_share);
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—';
});
const marketAssessment = computed(() => {
  const defensive = asTextList(marketMetrics.value.defensive_inflow_boards);
  const technologyOut = asTextList(marketMetrics.value.technology_outflow_boards);
  if (String(workspace.marketContent.market_state) === 'rotation_defensive') {
    return `资金偏向${defensive.slice(0, 5).join('、') || '防御方向'}；${technologyOut.slice(0, 5).join('、') || '科技方向'}承压。短线新开仓须等待个股和板块同时转强。`;
  }
  return '盘面方向必须与板块资金、个股量价触发同时确认，不能仅凭指数涨跌下单。';
});

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

function gateType(verdict: DecisionResearchGate['verdict']): 'success' | 'warning' | 'danger' | 'info' {
  if (verdict === 'pass') return 'success';
  if (verdict === 'fail') return 'danger';
  if (verdict === 'advisory') return 'warning';
  return 'info';
}

function gateLabel(verdict: DecisionResearchGate['verdict']): string {
  return ({ pass: '通过', fail: '否决', advisory: '风险提示', unknown: '证据不足' })[verdict];
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
        <div class="status-tile"><span>盘面分析</span><el-tag :type="workspace.brief.market.status === 'degraded' ? 'warning' : workspace.brief.delivery.market_eligible ? 'success' : 'danger'">{{ workspace.brief.market.status === 'degraded' ? '部分可用' : workspace.brief.delivery.market_eligible ? '完整' : '缺失' }}</el-tag></div>
        <div class="status-tile"><span>持仓操作</span><el-tag :type="workspace.brief.delivery.holding_actions_eligible ? 'success' : 'danger'">{{ workspace.brief.delivery.holding_actions_eligible ? '可用' : '阻断' }}</el-tag></div>
        <div class="status-tile"><span>新买计划</span><el-tag :type="workspace.brief.delivery.new_buy_actions_eligible ? 'success' : 'info'">{{ workspace.brief.delivery.new_buy_actions_eligible ? '有计划' : '无合格计划' }}</el-tag></div>
      </div>

      <el-card shadow="never" class="section-gap decision-section">
        <template #header><div class="section-title"><div><strong>市场与板块</strong><small>{{ displayValue(workspace.marketContent.observed_at || workspace.brief.as_of_at) }}</small></div><el-tag effect="plain">{{ stateLabel(workspace.marketContent.market_state) }}</el-tag></div></template>
        <el-empty v-if="!workspace.brief.delivery.market_eligible" description="没有可用的市场分析；这不会阻止已完成的新买计划显示" :image-size="52" />
        <template v-else>
          <el-alert v-if="workspace.brief.market.status === 'degraded'" title="当前板块资金证据可用，但指数或全市场涨跌广度不完整；以下结论只能作为板块轮动参考。" type="warning" :closable="false" show-icon class="market-warning" />
          <p class="market-assessment">{{ marketAssessment }}</p>
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="交易日">{{ displayValue(workspace.marketContent.exchange_date) }}</el-descriptions-item>
          <el-descriptions-item label="阶段">{{ displayValue(workspace.marketContent.session) }}</el-descriptions-item>
          <el-descriptions-item label="盘面状态">{{ stateLabel(workspace.marketContent.market_state) }}</el-descriptions-item>
          <el-descriptions-item label="已覆盖板块">{{ displayValue(marketMetrics.known_board_flows) }}</el-descriptions-item>
          <el-descriptions-item label="净流入板块占比">{{ positiveFlowPct }}</el-descriptions-item>
          <el-descriptions-item label="板块涨跌中位数">{{ numberText(marketMetrics.median_board_change_pct, 2) }}%</el-descriptions-item>
          <el-descriptions-item label="防御资金流入" :span="3">{{ asTextList(marketMetrics.defensive_inflow_boards).join('、') || '—' }}</el-descriptions-item>
          <el-descriptions-item label="科技资金流出" :span="3">{{ asTextList(marketMetrics.technology_outflow_boards).join('、') || '—' }}</el-descriptions-item>
          <el-descriptions-item label="多指数状态">{{ stateLabel(multiIndex.state) }}</el-descriptions-item>
          <el-descriptions-item label="有效指数数">{{ displayValue(multiIndex.index_count) }}</el-descriptions-item>
          <el-descriptions-item label="数据缺口">{{ marketQualityFlags.length ? marketQualityFlags.map(qualityLabel).join('；') : '无' }}</el-descriptions-item>
        </el-descriptions>
        </template>
      </el-card>

      <el-card shadow="never" class="section-gap decision-section">
        <template #header>
          <div class="section-title">
            <div><strong>研究审计</strong><small>每项用人能读懂的名称说明；内部编号只作追溯</small></div>
            <el-space v-if="workspace.research">
              <el-tag type="success">通过 {{ workspace.research.summary.passed }}</el-tag>
              <el-tag type="danger">否决 {{ workspace.research.summary.rejected }}</el-tag>
              <el-tag v-if="workspace.research.summary.incomplete" type="warning">未完成 {{ workspace.research.summary.incomplete }}</el-tag>
            </el-space>
          </div>
        </template>
        <el-alert v-if="workspace.researchError" :title="workspace.researchError" type="warning" :closable="false" show-icon />
        <el-empty v-else-if="!workspace.research?.items.length" description="尚无研究审计批次" :image-size="52" />
        <el-collapse v-else class="research-list">
          <el-collapse-item v-for="item in workspace.research.items" :key="item.dossier_key" :name="item.dossier_key">
            <template #title>
              <div class="research-heading">
                <span><strong>{{ item.name }}</strong>（{{ item.symbol }}）</span>
                <el-tag :type="item.status === 'passed' ? 'success' : item.status === 'rejected' ? 'danger' : 'warning'">
                  {{ item.status === 'passed' ? '研究通过' : item.status === 'rejected' ? '研究否决' : '证据不足' }}
                </el-tag>
                <small v-if="item.source_candidate_rank">扫描第 {{ item.source_candidate_rank }} 名</small>
              </div>
            </template>
            <p class="research-conclusion">{{ item.conclusion }}</p>
            <div v-for="gate in item.gates" :key="gate.gate_key" class="gate-row">
              <div class="gate-name"><strong>{{ gate.label }}</strong><small>{{ gate.gate_key }}<template v-if="gate.independent_run"> · 独立执行</template></small></div>
              <el-tag :type="gateType(gate.verdict)" effect="plain">{{ gateLabel(gate.verdict) }}</el-tag>
              <span>{{ gate.conclusion }}</span>
            </div>
          </el-collapse-item>
        </el-collapse>
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
            <el-descriptions-item label="策略仓位上限">{{ item.plan.max_position_pct }}%</el-descriptions-item>
            <el-descriptions-item label="有效期">{{ item.plan.valid_until }}</el-descriptions-item>
          </el-descriptions>
          <ul class="rationale"><li v-for="reason in item.plan.rationale" :key="reason">{{ reason }}</li></ul>
        </div>
      </el-card>

      <el-card shadow="never" class="section-gap decision-section">
        <template #header><div class="section-title"><div><strong>新买机会</strong><small>只显示已经形成完整进出场计划的标的</small></div><span>{{ workspace.brief.new_buys.actions?.length ?? 0 }} 项</span></div></template>
        <el-empty v-if="!workspace.brief.new_buys.actions?.length" description="当前没有满足完整研究与交易计划要求的新买标的" :image-size="52" />
        <div v-for="plan in workspace.brief.new_buys.actions" :key="plan.plan_key" class="action-card buy-card">
          <div class="action-heading"><div><strong>{{ plan.name }}</strong><span>（{{ plan.symbol }}）</span></div><el-space><ResearchOnlyBadge /><el-tag type="success">条件买入</el-tag></el-space></div>
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
.research-heading { display: flex; align-items: center; gap: 10px; width: 100%; padding-right: 12px; }
.research-heading small { margin-left: auto; color: var(--el-text-color-secondary); }
.research-conclusion { margin: 4px 0 14px; color: var(--el-text-color-regular); }
.market-warning { margin-bottom: 12px; }
.market-assessment { margin: 0 0 14px; padding: 12px 14px; border-left: 3px solid var(--el-color-primary); background: var(--el-fill-color-light); line-height: 1.65; }
.gate-row { display: grid; grid-template-columns: 180px 84px minmax(0, 1fr); align-items: start; gap: 12px; padding: 10px 0; border-top: 1px solid var(--el-border-color-lighter); }
.gate-name { display: flex; flex-direction: column; gap: 2px; }
.gate-name small { color: var(--el-text-color-secondary); }
@media (max-width: 900px) {
  .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .toolbar-row { align-items: flex-start; flex-direction: column; }
  .gate-row { grid-template-columns: 1fr 80px; }
  .gate-row > span { grid-column: 1 / -1; }
}
</style>

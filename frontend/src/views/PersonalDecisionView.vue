<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue';
import { computed, reactive, ref } from 'vue';
import { usePersonalDecisionWorkspace, type TradePlan } from '../composables/usePersonalDecisionWorkspace';
import ResearchOnlyBadge from '../components/ResearchOnlyBadge.vue';
import StockResearchWorkbench from '../components/StockResearchWorkbench.vue';
import EventResearchLive from '../components/EventResearchLive.vue';
import RecommendationPoolPanel from '../components/RecommendationPoolPanel.vue';
import DisciplineBoard from '../components/discipline/DisciplineBoard.vue';

const props = withDefaults(defineProps<{ mode?: 'market' | 'holdings' }>(), { mode: 'market' });
const isMarketView = computed(() => props.mode === 'market');
const isHoldingsView = computed(() => props.mode === 'holdings');
const workspace = reactive(usePersonalDecisionWorkspace(props.mode));
const holdingHasSnapshot = computed(() => Boolean(workspace.brief?.holdings?.portfolio_observed_at));
// News research is background context on the selection page: rendered (and
// polled) only when the reader opens it, so it never pushes candidates down.
const newsOpen = ref(false);

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

function scanTags(item: { tags?: Array<{ key: string; label: string; source: 'user' | 'strategy' }>; lane_labels?: string[] }) {
  if (item.tags?.length) return item.tags;
  return (item.lane_labels || []).map((label, index) => ({ key: `legacy:${index}:${label}`, label, source: 'strategy' as const }));
}

function trackingStanceLabel(value?: string): string {
  return ({
    strengthening: '多维度转强', mixed_watch: '方向尚未一致', risk_repair: '风险修复阶段',
  } as Record<string, string>)[String(value)] ?? '跟踪分析';
}

function trackingStanceType(value?: string): 'success' | 'warning' | 'danger' | 'info' {
  if (value === 'strengthening') return 'success';
  if (value === 'risk_repair') return 'danger';
  return value === 'mixed_watch' ? 'warning' : 'info';
}

function compactMoney(value: unknown): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  if (Math.abs(amount) >= 100_000_000) return `${(amount / 100_000_000).toFixed(2)}亿`;
  return `${(amount / 10_000).toFixed(0)}万`;
}
</script>

<template>
  <section class="personal-decision">
    <el-card shadow="never" class="decision-toolbar">
      <div class="toolbar-row">
        <div>
          <h2>{{ isMarketView ? '市场与选股' : '我的持仓' }}</h2>
          <p>{{ isMarketView ? '全市场扫描、盘面与新买研究；此页面不读取任何券商账户数据。' : '持仓按用户主动同步；本页刷新只读取已入库快照，不会操作交易软件。' }}</p>
        </div>
        <el-space wrap>
          <div v-if="isMarketView" class="chart-entry-card">
            <el-input v-model="workspace.chartSymbol" class="chart-symbol-input" placeholder="如 600487.SH" @keyup.enter="workspace.openChart(workspace.chartSymbol)">
              <template #prepend>按代码打开图形</template>
              <template #append><el-button @click="workspace.openChart(workspace.chartSymbol)">打开</el-button></template>
            </el-input>
          </div>
          <el-button type="primary" :icon="Refresh" :loading="workspace.loading" @click="workspace.load">刷新</el-button>
        </el-space>
      </div>
    </el-card>

    <el-alert v-if="workspace.error" :title="workspace.error" type="error" show-icon :closable="false" class="section-gap" />
    <el-skeleton v-if="workspace.loading && !workspace.brief" :rows="8" animated class="section-gap" />

    <template v-if="workspace.brief">
      <div v-if="isMarketView" class="status-grid section-gap independent-status-grid market-status-grid">
        <div class="status-tile"><span>盘面分析</span><el-tag :type="workspace.brief.market.status === 'degraded' ? 'warning' : workspace.brief.delivery.market_eligible ? 'success' : 'danger'">{{ workspace.brief.market.status === 'degraded' ? '部分可用' : workspace.brief.delivery.market_eligible ? '完整' : '缺失' }}</el-tag></div>
        <div class="status-tile"><span>全市场观察</span><el-tag :type="workspace.scanWatchlist?.items.length ? 'success' : workspace.scanError ? 'danger' : 'info'">{{ workspace.scanWatchlist?.items.length ? `${workspace.scanWatchlist.items.length} 只` : workspace.scanError ? '读取失败' : '暂无' }}</el-tag></div>
        <div class="status-tile"><span>正式条件重点</span><el-tag :type="workspace.formalRecommendation?.status === 'ready' ? 'success' : 'info'">{{ workspace.formalRecommendation?.status === 'ready' ? `${Array.isArray(workspace.formalRecommendation?.recommended) ? workspace.formalRecommendation.recommended.length : 0} 只` : '尚未发布' }}</el-tag></div>
      </div>
      <div v-else class="status-grid section-gap holdings-status-grid">
        <div class="status-tile"><span>账户持仓</span><el-tag :type="workspace.brief.delivery.holding_actions_eligible ? 'success' : 'danger'">{{ workspace.brief.delivery.holding_actions_eligible ? '当前且可用' : '同步不可用' }}</el-tag></div>
        <div class="status-tile"><span>精确读取时间</span><strong>{{ displayValue(workspace.brief.holdings.portfolio_observed_at) }}</strong></div>
      </div>

      <!-- 持仓页主区块：先今日动作汇总，再每只股票的纪律卡；下面的账户持仓建议为旧版文本计划。 -->
      <DisciplineBoard v-if="isHoldingsView" class="section-gap" :account-key="workspace.accountKey" :snapshot-observed-at="workspace.brief.holdings.portfolio_observed_at ?? null" />

      <!-- 选股优先：正式重点在最前，扫描候选紧随其后；盘面与消息只作背景，折叠在页面底部。 -->
      <RecommendationPoolPanel v-if="isMarketView" :value="workspace.formalRecommendation" />

      <el-card v-if="isMarketView" shadow="never" class="section-gap decision-section market-scan-section">
        <template #header>
          <div class="section-title">
            <div><strong>全市场扫描观察</strong><small>{{ displayValue(workspace.scanWatchlist?.as_of_date) }} 收盘扫描；与账户和持仓完全无关</small></div>
            <el-space><ResearchOnlyBadge /><span>{{ workspace.scanWatchlist?.total_unique ?? 0 }} 只去重候选</span></el-space>
          </div>
        </template>
        <el-alert v-if="workspace.scanError" :title="workspace.scanError" type="error" :closable="false" show-icon />
        <el-empty v-else-if="!workspace.scanWatchlist?.items.length" description="最新全市场扫描没有可展示的观察候选" :image-size="52" />
        <div v-else class="scan-grid">
          <article v-for="item in workspace.scanWatchlist.items" :key="item.symbol" class="scan-card" :class="`scan-${item.review_status}`">
            <div class="scan-heading">
              <div><strong>{{ item.name }}</strong><span>（{{ item.symbol }}）</span></div>
              <el-button plain size="small" @click="workspace.openChart(item.symbol)">查看图形</el-button>
            </div>
            <el-space wrap class="scan-tags"><el-tag v-for="tag in scanTags(item)" :key="tag.key" size="small" :type="tag.source === 'user' ? 'primary' : 'info'" :effect="tag.source === 'user' ? 'dark' : 'plain'">{{ tag.label }}</el-tag><el-tag size="small" :type="item.review_status === 'retain_watch' ? 'success' : item.review_status === 'downgrade_watch' || item.review_status === 'exclude' ? 'warning' : 'info'">{{ item.review_label }}</el-tag></el-space>
            <p class="scan-reason">{{ item.company_conclusion || item.reason || '策略筛选命中，等待下一交易日确认。' }}</p>
            <section v-if="item.user_requested_tracking && item.tracking_research" class="tracking-research">
              <div class="tracking-research-heading">
                <strong>主动跟踪分析</strong>
                <el-space><small>截至 {{ displayValue(item.tracking_research.as_of_date) }}</small><el-tag size="small" :type="trackingStanceType(item.tracking_research.stance)">{{ trackingStanceLabel(item.tracking_research.stance) }}</el-tag></el-space>
              </div>
              <p>{{ item.tracking_research.headline }}</p>
              <dl class="tracking-facts">
                <div><dt>收盘</dt><dd>{{ numberText(item.tracking_research.price_structure?.close, 2) }}</dd></div>
                <div><dt>5日资金</dt><dd>{{ compactMoney(item.tracking_research.capital_flow?.windows?.['5']?.net_amount) }}</dd></div>
                <div><dt>所属板块</dt><dd>{{ displayValue(item.tracking_research.sector?.label) }}</dd></div>
                <div><dt>换手率</dt><dd>{{ numberText(item.tracking_research.liquidity?.turnover_rate, 2) }}%</dd></div>
                <div><dt>PE / PB</dt><dd>{{ numberText(item.tracking_research.valuation?.pe, 1) }} / {{ numberText(item.tracking_research.valuation?.pb, 2) }}</dd></div>
                <div><dt>最近公告</dt><dd>{{ item.tracking_research.events?.[0]?.title || '未取得可追溯事件' }}</dd></div>
              </dl>
              <dl class="tracking-conditions">
                <div><dt>转强确认</dt><dd>{{ displayValue(item.tracking_research.conditions?.confirmation) }}</dd></div>
                <div><dt>继续观察</dt><dd>{{ displayValue(item.tracking_research.conditions?.range) }}</dd></div>
                <div><dt>跟踪失效</dt><dd>{{ displayValue(item.tracking_research.conditions?.invalidation) }}</dd></div>
              </dl>
              <el-alert v-if="item.tracking_research.status === 'degraded'" :title="`部分证据不可用：${item.tracking_research.unavailable_sections?.join('、') || '未知'}`" type="warning" :closable="false" />
            </section>
            <dl class="scan-levels"><div><dt>观察成立</dt><dd>{{ displayValue(item.confirmation) }}</dd></div><div><dt>观察失效</dt><dd>{{ displayValue(item.invalidation) }}</dd></div></dl>
          </article>
        </div>
      </el-card>

      <div v-if="isMarketView" class="context-grid section-gap">
      <el-card shadow="never" class="decision-section market-context-card">
        <template #header><div class="section-title"><div><strong>市场与板块</strong><small>背景参考 · {{ displayValue(workspace.marketContent.observed_at || workspace.brief.as_of_at) }}</small></div><el-tag effect="plain">{{ stateLabel(workspace.marketContent.market_state) }}</el-tag></div></template>
        <el-alert v-if="workspace.marketError" :title="workspace.marketError" type="error" :closable="false" show-icon />
        <el-empty v-if="!workspace.brief.delivery.market_eligible" description="没有可用的市场分析；这不会阻止已完成的新买计划显示" :image-size="52" />
        <template v-else>
          <el-alert v-if="workspace.brief.market.status === 'degraded'" title="当前板块资金证据可用，但指数或全市场涨跌广度不完整；以下结论只能作为板块轮动参考。" type="warning" :closable="false" show-icon class="market-warning" />
          <p class="market-assessment">{{ marketAssessment }}</p>
        <details class="context-details">
        <summary>完整盘面指标</summary>
        <el-descriptions :column="2" border size="small">
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
        </details>
        </template>
      </el-card>

      <el-card shadow="never" class="decision-section news-context-card">
        <template #header><div class="section-title"><div><strong>消息与事件</strong><small>背景参考 · 展开后每分钟自动刷新</small></div><el-button size="small" plain @click="newsOpen = !newsOpen">{{ newsOpen ? '收起' : '展开消息研究' }}</el-button></div></template>
        <EventResearchLive v-if="newsOpen" />
        <p v-else class="context-hint">消息研究不进入选股结论，只解释方向；需要时再展开，避免挤占候选区域。</p>
      </el-card>
      </div>

      <el-card v-if="isHoldingsView" shadow="never" class="section-gap decision-section holdings-only-section">
        <template #header><div class="section-title"><div><strong>账户持仓建议</strong><small>独立券商链；持仓快照 {{ displayValue(workspace.brief.holdings.portfolio_observed_at) }}</small></div><el-space><el-input v-model="workspace.accountKey" aria-label="账户标识" class="account-input" @keyup.enter="workspace.load" /><span>{{ workspace.brief.holdings.actions?.length ?? 0 }} 项</span></el-space></div></template>
        <el-alert v-if="workspace.holdingError" :title="workspace.holdingError" type="error" :closable="false" show-icon />
        <el-alert v-if="workspace.brief.holdings.freshness_status !== 'current' && holdingHasSnapshot" :title="`持仓已过期，请登录电脑交易账户并主动同步。持仓实际读取时间：${displayValue(workspace.brief.holdings.portfolio_observed_at)}；市场参考日：${displayValue(workspace.brief.holdings.reference_trade_date)}。旧持仓、数量、成本和操作线均不参与本页其他建议。`" type="error" show-icon :closable="false" class="holding-stale-alert" />
        <el-alert v-else-if="workspace.brief.holdings.freshness_status !== 'current'" title="尚未读取到持仓。请登录电脑交易账户并主动同步，本页刷新只读取已入库快照。" type="warning" show-icon :closable="false" class="holding-stale-alert" />
        <el-alert v-if="!workspace.brief.delivery.holding_actions_eligible" title="没有同时满足“精确持仓快照 + 完整交易计划”的持仓动作，系统不会用旧持仓或纸面账户代替。" type="warning" :closable="false" show-icon />
        <div v-for="item in workspace.brief.holdings.actions" :key="item.plan.plan_key" class="action-card">
          <div class="action-heading">
            <div><strong>{{ item.position.name }}</strong><span>（{{ item.position.symbol }}）</span></div>
            <el-space><el-button type="primary" plain size="small" class="chart-launch" @click="workspace.openChart(item.position.symbol)">查看图形</el-button><el-tag :type="actionType(item.plan.action)">{{ actionLabel(item.plan.action) }}</el-tag></el-space>
          </div>
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

      <el-collapse v-if="workspace.brief.diagnostics?.length" class="section-gap diagnostics">
        <el-collapse-item title="内部诊断" name="diagnostics"><el-tag v-for="item in workspace.brief.diagnostics" :key="item" type="warning" effect="plain" class="diagnostic-tag">{{ item }}</el-tag></el-collapse-item>
      </el-collapse>
    </template>

    <el-drawer v-model="workspace.chartOpen" size="min(1500px, 96vw)" direction="rtl" destroy-on-close class="decision-chart-drawer" @closed="workspace.closeChart">
      <template #header>
        <div class="chart-drawer-title"><strong>图形决策工作台</strong><span>{{ workspace.chartSymbol || '正在读取' }} · 120 日日线</span></div>
      </template>
      <el-skeleton v-if="workspace.chartLoading" :rows="12" animated />
      <el-alert v-else-if="workspace.chartError" :title="workspace.chartError" type="error" :closable="false" show-icon />
      <StockResearchWorkbench v-else-if="workspace.chartWorkbench" :workbench="workspace.chartWorkbench" />
      <el-empty v-else description="没有可用图形数据" />
    </el-drawer>
  </section>
</template>

<style scoped>
.personal-decision { max-width: 1440px; margin: 0 auto; }
.decision-toolbar h2 { margin: 0 0 5px; font-size: 20px; }
.decision-toolbar p { margin: 0; color: var(--el-text-color-secondary); }
.toolbar-row, .section-title, .action-heading { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.account-input { width: 190px; }
.status-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.market-status-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.holdings-status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.status-tile { display: flex; align-items: center; justify-content: space-between; padding: 15px 16px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: var(--el-bg-color); }
.section-title > div { display: flex; flex-direction: column; gap: 3px; }
.section-title small { color: var(--el-text-color-secondary); font-weight: 400; }
.action-card { padding: 15px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: var(--el-fill-color-blank); }
.action-card + .action-card { margin-top: 12px; }
.buy-card { border-left: 3px solid var(--el-color-success); }
.stock-advice-section { border-top: 2px solid var(--el-color-primary-light-5); }
.holding-stale-alert { margin-bottom: 10px; }
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
.chart-drawer-title { display: flex; flex-direction: column; gap: 3px; }
.chart-drawer-title strong { font-size: 18px; }
.chart-drawer-title span { color: var(--el-text-color-secondary); font-size: 12px; }
.chart-symbol-input { width: 300px; }
.context-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; }
.market-context-card, .news-context-card { border-top: 2px solid var(--el-border-color); }
.context-details { margin-top: 4px; }
.context-details summary { cursor: pointer; color: var(--el-text-color-secondary); font-size: 13px; margin-bottom: 10px; }
.context-hint { margin: 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.market-scan-section { border-top: 2px solid var(--el-color-success-light-5); }
.scan-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.scan-card { padding: 14px; border: 1px solid var(--el-border-color-lighter); border-left: 3px solid var(--el-color-info); border-radius: 8px; background: var(--el-fill-color-blank); }
.scan-card.scan-retain_watch { border-left-color: var(--el-color-success); }
.scan-card.scan-downgrade_watch { border-left-color: var(--el-color-warning); }
.scan-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.scan-heading strong { font-size: 16px; }
.scan-heading span { color: var(--el-text-color-secondary); }
.tracking-research { margin: 12px 0; padding: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: var(--el-fill-color-light); }
.tracking-research-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.tracking-research-heading small { color: var(--el-text-color-secondary); }
.tracking-research > p { margin: 9px 0; line-height: 1.6; }
.tracking-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.tracking-facts > div, .tracking-conditions > div { min-width: 0; }
.tracking-facts dt, .tracking-conditions dt { color: var(--el-text-color-secondary); font-size: 12px; }
.tracking-facts dd, .tracking-conditions dd { margin: 3px 0 0; line-height: 1.45; overflow-wrap: anywhere; }
.tracking-conditions { display: grid; gap: 8px; margin: 12px 0 0; padding-top: 10px; border-top: 1px solid var(--el-border-color-lighter); }
.scan-tags { margin-top: 9px; }
.scan-reason { margin: 10px 0; color: var(--el-text-color-regular); line-height: 1.6; }
.scan-levels { display: grid; gap: 6px; margin: 0; font-size: 13px; }
.scan-levels div { display: grid; grid-template-columns: 68px minmax(0, 1fr); gap: 8px; }
.scan-levels dt { color: var(--el-text-color-secondary); }
.scan-levels dd { margin: 0; line-height: 1.5; }
@media (max-width: 900px) {
  .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .toolbar-row { align-items: flex-start; flex-direction: column; }
  .gate-row { grid-template-columns: 1fr 80px; }
  .gate-row > span { grid-column: 1 / -1; }
  .chart-symbol-input { width: 100%; }
  .context-grid { grid-template-columns: 1fr; }
  .scan-grid { grid-template-columns: 1fr; }
}
</style>

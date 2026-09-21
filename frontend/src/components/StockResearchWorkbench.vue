<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import '../charts/registerResearchCharts';
import VChart from 'vue-echarts';
import { guanshiChartTheme } from '../theme/chart-theme';
import type { MetricKey, StockWorkbench } from './stock-workbench';
import { displayPlanValue, metricChartOption, metricLabel, priceChartOption, relevantMessages, strategyMetrics, tradePlanAnnotations } from './stock-workbench';
import type { StockWorkbenchControl, WorkbenchAnnotation, WorkbenchPanel } from './stock-workbench-control';
import { isLiveWorkbenchControl, panelIsVisible } from './stock-workbench-control';

const props = defineProps<{ workbench: StockWorkbench; control?: StockWorkbenchControl | null; thesisAnnotations?: WorkbenchAnnotation[] }>();
const activeStrategy = ref(props.workbench.strategies[0]?.key ?? 'accumulation');
const timeframe = ref<'daily' | 'weekly'>('daily');
const activeMetric = ref<MetricKey>('vendor_flow');
const zoomStart = ref(55);
const zoomEnd = ref(100);

const liveControl = computed(() => isLiveWorkbenchControl(props.control) && (!props.control.symbol || props.control.symbol === props.workbench.symbol) ? props.control : null);
const strategy = computed(() => props.workbench.strategies.find((item) => item.key === activeStrategy.value));
const view = computed(() => props.workbench.strategy_views.find((item) => item.key === activeStrategy.value));
const bars = computed(() => props.workbench.series[timeframe.value]);
const metricChoices = computed(() => {
  const choices = strategyMetrics(strategy.value);
  const supplemental = liveControl.value?.metric;
  return supplemental && !choices.some((item) => item.key === supplemental)
    ? [...choices, { key: supplemental, label: `${metricLabel(supplemental)} · Agent补充` }]
    : choices;
});
const messages = computed(() => relevantMessages(props.workbench.messages, activeStrategy.value));
const chartAnnotations = computed(() => [...tradePlanAnnotations(props.workbench), ...(props.thesisAnnotations ?? []), ...(liveControl.value?.annotations ?? [])]);
const priceOption = computed(() => priceChartOption(bars.value, strategy.value, view.value, messages.value, zoomStart.value, zoomEnd.value, chartAnnotations.value));
const metricOption = computed(() => metricChartOption(bars.value, props.workbench, activeMetric.value));
const requiredHealth = computed(() => (strategy.value?.required_panels ?? []).map((key) => ({ key, ...(props.workbench.data_health[key] ?? { status: 'unavailable', detail: '未声明' }) })));
const narrationAnnotations = computed(() => liveControl.value?.annotations.filter((item) => item.kind === 'note') ?? []);
const freshnessWarnings = computed(() => Object.entries(props.workbench.artifact_freshness ?? {})
  .filter(([, value]) => !['ready', 'current'].includes(value.status))
  .map(([key, value]) => `${evidenceLabel(key)}：${value.detail || evidenceLabel(value.status)}`));

function evidenceLabel(key: string) {
  const labels: Record<string, string> = {
    price: '价格', volume: '成交量', vendor_flow: '资金口径', turnover: '换手', sector: '板块',
    messages: '消息', scenario: '情景', trade_plan: '交易计划', history: '行情历史',
    ready: '可用', current: '当前有效', partial: '部分可用', unavailable: '暂无数据',
    stale: '已过期', unknown: '状态未明', mixed_rotation: '轮动分化', neutral: '中性',
  };
  return labels[key] || key;
}

watch(activeStrategy, () => {
  const requested = liveControl.value?.strategy_key === activeStrategy.value ? liveControl.value.metric : null;
  activeMetric.value = requested ?? metricChoices.value[0]?.key ?? 'volume';
});

watch(() => [props.workbench.symbol, props.control?.revision] as const, () => {
  const control = liveControl.value;
  if (!control) return;
  const controlledStrategy = props.workbench.strategies.find((item) => item.key === control.strategy_key);
  if (controlledStrategy) {
    activeStrategy.value = controlledStrategy.key;
    if (control.metric) activeMetric.value = control.metric;
    else activeMetric.value = strategyMetrics(controlledStrategy)[0]?.key ?? 'volume';
  } else if (control.metric) activeMetric.value = control.metric;
  timeframe.value = control.timeframe;
  zoomStart.value = control.zoom.start;
  zoomEnd.value = control.zoom.end;
}, { immediate: true });

function showPanel(panel: WorkbenchPanel) {
  return panelIsVisible(liveControl.value, panel);
}

function isFocused(scope: 'next_session' | 'next_week', state: string) {
  return liveControl.value?.focus?.scope === scope && liveControl.value.focus.state === state;
}

function onZoom(rawPayload: unknown) {
  const payload = rawPayload as { start?: number; end?: number; batch?: Array<{ start?: number; end?: number }> };
  const value = payload.batch?.[0]?.start ?? payload.start;
  const end = payload.batch?.[0]?.end ?? payload.end;
  if (typeof value === 'number') zoomStart.value = value;
  if (typeof end === 'number') zoomEnd.value = end;
}

function statusClass(status?: string) {
  return status === 'ready' ? 'is-ready' : status === 'checked_no_signal' ? 'is-empty' : 'is-warn';
}

function money(value: unknown) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  if (Math.abs(amount) >= 100_000_000) return `${(amount / 100_000_000).toFixed(2)}亿`;
  return `${(amount / 10_000).toFixed(0)}万`;
}
</script>

<template>
  <section class="workbench-shell" :class="{ 'is-agent-controlled': liveControl }">
    <header class="workbench-header">
      <div>
        <p class="eyebrow" :title="workbench.contract_version">个股研判</p>
        <h2>{{ workbench.name }} <span>{{ workbench.symbol }}</span></h2>
        <p>{{ workbench.industry || '行业待映射' }} · 数据截至 {{ workbench.as_of_date }}</p>
      </div>
      <div class="header-state">
        <span>{{ workbench.series.daily.length }} 根日线</span>
        <span :class="statusClass(workbench.source_health.history?.status)">{{ evidenceLabel(workbench.source_health.history?.status || 'unknown') }}</span>
      </div>
    </header>

    <div v-if="freshnessWarnings.length" class="freshness-warning">
      <strong>部分证据不是当前有效数据</strong>
      <span v-for="item in freshnessWarnings" :key="item">{{ item }}</span>
    </div>

    <Transition name="agent-note">
      <aside v-if="liveControl?.speaker_note" :key="liveControl.revision" class="agent-narration">
        <div><p class="eyebrow">随看随记 · 仅控制演示</p><h3>{{ liveControl.speaker_note.title }}</h3></div>
        <p>{{ liveControl.speaker_note.body }}</p>
        <small>{{ [liveControl.speaker_note.source_label, liveControl.speaker_note.as_of].filter(Boolean).join(' · ') || '当前对话临时讲解' }}</small>
      </aside>
    </Transition>

    <nav class="strategy-tabs" aria-label="短线策略">
      <button v-for="item in workbench.strategies" :key="item.key" type="button" :class="{ active: activeStrategy === item.key }" @click="activeStrategy = item.key">
        <strong>{{ item.label }}</strong><small>{{ item.thesis }}</small>
      </button>
    </nav>

    <Transition name="strategy-fade" mode="out-in">
      <div :key="activeStrategy" class="strategy-context">
        <div class="reading-block">
          <span class="state-pill" :class="statusClass(view?.status)">{{ view?.status === 'ready' ? '证据齐备' : '证据降级' }}</span>
          <p v-for="line in view?.current_reading ?? []" :key="line">{{ line }}</p>
        </div>
        <div class="health-strip">
          <span v-for="item in requiredHealth" :key="item.key" :class="statusClass(item.status)" :title="item.detail">{{ evidenceLabel(item.key) }}</span>
        </div>
        <div v-if="view?.regime_route || view?.risk_envelope" class="risk-strip">
          <span v-if="view?.regime_route">市场环境 {{ evidenceLabel(view.regime_route.regime) }} · {{ evidenceLabel(view.regime_route.state) }}</span>
          <span v-if="view?.risk_envelope">研究仓位上限 {{ view.risk_envelope.max_single_position_pct }}% · 行业 {{ view.risk_envelope.max_sector_exposure_pct }}%</span>
          <span v-if="view?.risk_envelope">结构失效参考 {{ view.risk_envelope.failure_reference }}</span>
        </div>
      </div>
    </Transition>

    <div class="workbench-grid">
      <div class="charts-column">
        <article v-if="showPanel('price')" class="panel chart-panel">
          <div class="panel-heading">
            <div><p class="eyebrow">观势</p><h3>K线与策略参考线</h3></div>
            <div class="segmented"><button :class="{ active: timeframe === 'daily' }" @click="timeframe = 'daily'">日线</button><button :class="{ active: timeframe === 'weekly' }" @click="timeframe = 'weekly'">周线</button></div>
          </div>
          <VChart :theme="guanshiChartTheme" class="price-chart" :option="priceOption" autoresize @datazoom="onZoom" />
          <p class="chart-footnote">蓝色为结构支撑，橙色为压力参考，红色为策略失效参考；三者都不是自动止损。</p>
          <div v-if="chartAnnotations.length" class="annotation-ledger">
            <span v-for="item in chartAnnotations" :key="item.id" :style="{ borderColor: item.color || '#22d3ee' }" :title="item.detail">{{ item.label }}</span>
          </div>
        </article>

        <article v-if="showPanel('metric')" class="panel metric-panel">
          <div class="panel-heading">
            <div><p class="eyebrow">旁证</p><h3>当前策略需要的辅助证据</h3></div>
            <select v-model="activeMetric" aria-label="辅助指标"><option v-for="item in metricChoices" :key="item.key" :value="item.key">{{ item.label }}</option></select>
          </div>
          <VChart :theme="guanshiChartTheme" class="metric-chart" :option="metricOption" autoresize />
          <p v-if="activeMetric === 'vendor_flow'" class="chart-footnote">{{ workbench.flow.semantic_boundary }}</p>
        </article>
      </div>

      <aside class="decision-column">
        <article v-if="showPanel('next_session')" class="panel scenario-panel">
          <div class="panel-heading"><div><p class="eyebrow">明日推演</p><h3>下一交易日三种路径</h3></div><span class="muted">不报伪概率</span></div>
          <div v-for="scenario in view?.next_session ?? []" :key="scenario.state" class="scenario-card" :class="{ 'agent-focus': isFocused('next_session', scenario.state) }">
            <h4>{{ scenario.state }}</h4><p><b>出现什么：</b>{{ scenario.condition }}</p><p><b>如何处理：</b>{{ scenario.action }}</p><p v-if="scenario.invalidation"><b>何时不再成立：</b>{{ scenario.invalidation }}</p>
          </div>
        </article>

        <article v-if="showPanel('next_week')" class="panel scenario-panel week-panel">
          <div class="panel-heading"><div><p class="eyebrow">下周推演</p><h3>下周情景与动作</h3></div></div>
          <div v-for="scenario in view?.next_week ?? []" :key="scenario.state" class="scenario-card compact" :class="{ 'agent-focus': isFocused('next_week', scenario.state) }">
            <h4>{{ scenario.state }}</h4><p>{{ scenario.condition }}</p><p><b>动作：</b>{{ scenario.action }}</p><div class="checkpoint-row"><span v-for="point in scenario.checkpoints ?? []" :key="point">{{ point }}</span></div>
          </div>
          <p class="probability-note">{{ view?.probability_note }}</p>
        </article>
      </aside>
    </div>

    <div class="detail-grid">
      <article v-if="showPanel('messages')" class="panel message-panel">
        <div class="panel-heading"><div><p class="eyebrow">消息脉络</p><h3>与当前策略相关的消息</h3></div><span>{{ messages.length }} 条</span></div>
        <div v-if="messages.length" class="timeline">
          <a v-for="message in messages" :key="message.id" :href="message.url || undefined" :target="message.url ? '_blank' : undefined" class="timeline-item">
            <time>{{ message.occurred_at?.slice(0, 10) || '-' }}</time><div><h4>{{ message.title }}</h4><p>{{ message.detail || message.impact }}</p><small>{{ message.source }} · {{ message.verification }} · {{ message.category }}</small></div>
          </a>
        </div><p v-else class="empty-state">当前没有取得可追溯事件，无法区分“确实没有消息”和“消息采集未覆盖”；不能据此判断公司没有风险。</p>
      </article>

      <article v-if="showPanel('trade_plan')" class="panel plan-panel">
        <div class="panel-heading"><div><p class="eyebrow">取舍</p><h3>当前有效交易计划</h3></div></div>
        <template v-if="workbench.active_trade_plan">
          <div class="plan-hero"><strong>{{ workbench.active_trade_plan.action }}</strong><span>最大仓位 {{ workbench.active_trade_plan.max_position_pct }}%</span></div>
          <dl><dt>入场区间</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.entry_zone) }}</dd><dt>加仓条件</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.add_trigger) }}</dd><dt>减仓条件</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.reduce_trigger) }}</dd><dt>退出条件</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.exit_trigger) }}</dd><dt>止损参考</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.stop_price) }}</dd><dt>目标</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.target_prices) }}</dd></dl>
        </template>
        <div v-else class="empty-plan"><strong>没有与最新行情一致的有效交易计划</strong><p>{{ workbench.artifact_freshness?.trade_plan?.detail || '图上的支撑、压力和情景只用于决定是否启动或更新研究，不能直接当作买入许可。' }}</p></div>
        <div class="flow-summary"><span>1日 {{ money(workbench.flow.windows?.['1']?.net_amount) }}</span><span>5日 {{ money(workbench.flow.windows?.['5']?.net_amount) }}</span><span>10日 {{ money(workbench.flow.windows?.['10']?.net_amount) }}</span></div>
      </article>
      <article v-if="narrationAnnotations.length" class="panel narration-ledger">
        <div class="panel-heading"><div><p class="eyebrow">批注</p><h3>对话中添加的解释</h3></div><span>不写入研究事实</span></div>
        <div v-for="item in narrationAnnotations" :key="item.id" class="narration-item"><strong>{{ item.label }}</strong><p>{{ item.detail }}</p><small>{{ [item.source_label, item.as_of].filter(Boolean).join(' · ') || '临时标注' }}</small></div>
      </article>
    </div>
  </section>
</template>

<style scoped src="./stock-workbench.css"></style>

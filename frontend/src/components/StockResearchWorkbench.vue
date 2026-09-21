<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import '../charts/registerResearchCharts';
import VChart from 'vue-echarts';
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
  .map(([key, value]) => `${key}: ${value.detail || value.status}`));

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
        <p class="eyebrow">策略研究工作台 · {{ workbench.contract_version }}</p>
        <h2>{{ workbench.name }} <span>{{ workbench.symbol }}</span></h2>
        <p>{{ workbench.industry || '行业待映射' }} · 数据截至 {{ workbench.as_of_date }}</p>
      </div>
      <div class="header-state">
        <span>{{ workbench.series.daily.length }} 根日线</span>
        <span :class="statusClass(workbench.source_health.history?.status)">{{ workbench.source_health.history?.status || 'unknown' }}</span>
      </div>
    </header>

    <div v-if="freshnessWarnings.length" class="freshness-warning">
      <strong>部分证据不是当前有效数据</strong>
      <span v-for="item in freshnessWarnings" :key="item">{{ item }}</span>
    </div>

    <Transition name="agent-note">
      <aside v-if="liveControl?.speaker_note" :key="liveControl.revision" class="agent-narration">
        <div><p class="eyebrow">AGENT WALKTHROUGH · 仅控制演示</p><h3>{{ liveControl.speaker_note.title }}</h3></div>
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
          <span v-for="item in requiredHealth" :key="item.key" :class="statusClass(item.status)" :title="item.detail">{{ item.key }}</span>
        </div>
        <div v-if="view?.regime_route || view?.risk_envelope" class="risk-strip">
          <span v-if="view?.regime_route">市场路由 {{ view.regime_route.regime }} · {{ view.regime_route.state }}</span>
          <span v-if="view?.risk_envelope">研究仓位上限 {{ view.risk_envelope.max_single_position_pct }}% · 行业 {{ view.risk_envelope.max_sector_exposure_pct }}%</span>
          <span v-if="view?.risk_envelope">结构失效参考 {{ view.risk_envelope.failure_reference }}</span>
        </div>
      </div>
    </Transition>

    <div class="workbench-grid">
      <div class="charts-column">
        <article v-if="showPanel('price')" class="panel chart-panel">
          <div class="panel-heading">
            <div><p class="eyebrow">PRICE STRUCTURE</p><h3>K线与策略参考线</h3></div>
            <div class="segmented"><button :class="{ active: timeframe === 'daily' }" @click="timeframe = 'daily'">日线</button><button :class="{ active: timeframe === 'weekly' }" @click="timeframe = 'weekly'">周线</button></div>
          </div>
          <VChart class="price-chart" :option="priceOption" autoresize @datazoom="onZoom" />
          <p class="chart-footnote">蓝色为结构支撑，橙色为压力参考，红色为策略失效参考；三者都不是自动止损。</p>
          <div v-if="chartAnnotations.length" class="annotation-ledger">
            <span v-for="item in chartAnnotations" :key="item.id" :style="{ borderColor: item.color || '#22d3ee' }" :title="item.detail">{{ item.label }}</span>
          </div>
        </article>

        <article v-if="showPanel('metric')" class="panel metric-panel">
          <div class="panel-heading">
            <div><p class="eyebrow">STRATEGY EVIDENCE</p><h3>当前策略需要的辅助证据</h3></div>
            <select v-model="activeMetric" aria-label="辅助指标"><option v-for="item in metricChoices" :key="item.key" :value="item.key">{{ item.label }}</option></select>
          </div>
          <VChart class="metric-chart" :option="metricOption" autoresize />
          <p v-if="activeMetric === 'vendor_flow'" class="chart-footnote">{{ workbench.flow.semantic_boundary }}</p>
        </article>
      </div>

      <aside class="decision-column">
        <article v-if="showPanel('next_session')" class="panel scenario-panel">
          <div class="panel-heading"><div><p class="eyebrow">NEXT SESSION</p><h3>下一交易日三种路径</h3></div><span class="muted">不报伪概率</span></div>
          <div v-for="scenario in view?.next_session ?? []" :key="scenario.state" class="scenario-card" :class="{ 'agent-focus': isFocused('next_session', scenario.state) }">
            <h4>{{ scenario.state }}</h4><p><b>出现什么：</b>{{ scenario.condition }}</p><p><b>如何处理：</b>{{ scenario.action }}</p><p v-if="scenario.invalidation"><b>何时不再成立：</b>{{ scenario.invalidation }}</p>
          </div>
        </article>

        <article v-if="showPanel('next_week')" class="panel scenario-panel week-panel">
          <div class="panel-heading"><div><p class="eyebrow">NEXT WEEK</p><h3>下周情景与动作</h3></div></div>
          <div v-for="scenario in view?.next_week ?? []" :key="scenario.state" class="scenario-card compact" :class="{ 'agent-focus': isFocused('next_week', scenario.state) }">
            <h4>{{ scenario.state }}</h4><p>{{ scenario.condition }}</p><p><b>动作：</b>{{ scenario.action }}</p><div class="checkpoint-row"><span v-for="point in scenario.checkpoints ?? []" :key="point">{{ point }}</span></div>
          </div>
          <p class="probability-note">{{ view?.probability_note }}</p>
        </article>
      </aside>
    </div>

    <div class="detail-grid">
      <article v-if="showPanel('messages')" class="panel message-panel">
        <div class="panel-heading"><div><p class="eyebrow">EVENT TIMELINE</p><h3>与当前策略相关的消息</h3></div><span>{{ messages.length }} 条</span></div>
        <div v-if="messages.length" class="timeline">
          <a v-for="message in messages" :key="message.id" :href="message.url || undefined" :target="message.url ? '_blank' : undefined" class="timeline-item">
            <time>{{ message.occurred_at?.slice(0, 10) || '-' }}</time><div><h4>{{ message.title }}</h4><p>{{ message.detail || message.impact }}</p><small>{{ message.source }} · {{ message.verification }} · {{ message.category }}</small></div>
          </a>
        </div><p v-else class="empty-state">当前没有取得可追溯事件，无法区分“确实没有消息”和“消息采集未覆盖”；不能据此判断公司没有风险。</p>
      </article>

      <article v-if="showPanel('trade_plan')" class="panel plan-panel">
        <div class="panel-heading"><div><p class="eyebrow">ACTIVE PLAN</p><h3>当前有效交易计划</h3></div></div>
        <template v-if="workbench.active_trade_plan">
          <div class="plan-hero"><strong>{{ workbench.active_trade_plan.action }}</strong><span>最大仓位 {{ workbench.active_trade_plan.max_position_pct }}%</span></div>
          <dl><dt>入场区间</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.entry_zone) }}</dd><dt>加仓条件</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.add_trigger) }}</dd><dt>减仓条件</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.reduce_trigger) }}</dd><dt>退出条件</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.exit_trigger) }}</dd><dt>止损参考</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.stop_price) }}</dd><dt>目标</dt><dd>{{ displayPlanValue(workbench.active_trade_plan.target_prices) }}</dd></dl>
        </template>
        <div v-else class="empty-plan"><strong>没有与最新行情一致的有效交易计划</strong><p>{{ workbench.artifact_freshness?.trade_plan?.detail || '图上的支撑、压力和情景只用于决定是否启动或更新研究，不能直接当作买入许可。' }}</p></div>
        <div class="flow-summary"><span>1日 {{ money(workbench.flow.windows?.['1']?.net_amount) }}</span><span>5日 {{ money(workbench.flow.windows?.['5']?.net_amount) }}</span><span>10日 {{ money(workbench.flow.windows?.['10']?.net_amount) }}</span></div>
      </article>
      <article v-if="narrationAnnotations.length" class="panel narration-ledger">
        <div class="panel-heading"><div><p class="eyebrow">TEMPORARY NOTES</p><h3>对话中添加的解释</h3></div><span>不写入研究事实</span></div>
        <div v-for="item in narrationAnnotations" :key="item.id" class="narration-item"><strong>{{ item.label }}</strong><p>{{ item.detail }}</p><small>{{ [item.source_label, item.as_of].filter(Boolean).join(' · ') || '临时标注' }}</small></div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.workbench-shell{--bg:#07111f;--panel:#0d1b2b;--line:rgba(148,163,184,.16);--text:#e8eef8;--muted:#8da0b8;--accent:#50a9ff;color:var(--text);background:radial-gradient(circle at 12% 0%,rgba(38,123,189,.18),transparent 28%),var(--bg);border-radius:18px;padding:22px;min-height:720px}.workbench-shell.is-agent-controlled{box-shadow:inset 0 0 0 1px rgba(34,211,238,.32),0 18px 60px rgba(5,18,31,.2)}.workbench-header,.panel-heading,.strategy-context,.plan-hero,.flow-summary{display:flex;align-items:center;justify-content:space-between;gap:16px}.workbench-header h2{font-size:25px;margin:3px 0}.workbench-header h2 span{font-size:13px;font-weight:500;color:var(--muted);margin-left:8px}.workbench-header p,.muted,.chart-footnote,.probability-note{color:var(--muted)}.eyebrow{font-size:10px!important;letter-spacing:.16em;margin:0!important;color:#65b8ff!important}.header-state{display:flex;gap:8px}.header-state span,.health-strip span,.state-pill,.checkpoint-row span{border:1px solid var(--line);border-radius:999px;padding:5px 9px;font-size:11px;background:rgba(255,255,255,.03)}.agent-narration{display:grid;grid-template-columns:minmax(180px,.5fr) minmax(280px,1.5fr) auto;gap:20px;align-items:center;margin:16px 0 4px;padding:14px 16px;border:1px solid rgba(34,211,238,.35);border-radius:12px;background:linear-gradient(100deg,rgba(8,145,178,.18),rgba(15,31,49,.65))}.agent-narration h3,.agent-narration p{margin:3px 0}.agent-narration>p{font-size:12px;line-height:1.6;color:#d2e6ee}.agent-narration small{color:#77bfc9;white-space:nowrap}.is-ready{color:#63d8ad!important;border-color:rgba(99,216,173,.4)!important}.is-warn{color:#f7bf6a!important;border-color:rgba(247,191,106,.4)!important}.is-empty{color:#92a3b9!important}.strategy-tabs{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin:20px 0 12px}.strategy-tabs button{min-width:0;text-align:left;padding:12px;border:1px solid var(--line);border-radius:12px;background:#0a1726;color:var(--text);cursor:pointer;transition:transform .18s ease,border-color .18s ease,background .18s ease}.strategy-tabs button:hover{transform:translateY(-1px);border-color:#3d7aaa}.strategy-tabs button.active{background:linear-gradient(135deg,rgba(52,132,199,.35),rgba(25,63,96,.3));border-color:#4ea6e8;box-shadow:inset 0 0 0 1px rgba(78,166,232,.18)}.strategy-tabs strong,.strategy-tabs small{display:block}.strategy-tabs small{color:var(--muted);font-size:10px;line-height:1.4;margin-top:5px}.strategy-context{min-height:58px;border:1px solid var(--line);border-radius:12px;background:rgba(13,27,43,.88);padding:12px 14px;margin-bottom:12px}.reading-block{display:flex;align-items:center;gap:15px;flex-wrap:wrap}.reading-block p{margin:0;color:#c7d4e5;font-size:12px}.health-strip{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}.health-strip span{text-transform:uppercase}.workbench-grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(330px,.85fr);gap:12px}.charts-column,.decision-column{display:grid;gap:12px;align-content:start}.panel{border:1px solid var(--line);border-radius:14px;background:linear-gradient(180deg,rgba(15,31,49,.98),rgba(10,22,36,.98));padding:15px;box-shadow:0 10px 35px rgba(0,0,0,.16)}.panel h3{font-size:15px;margin:3px 0}.panel-heading>span{font-size:11px;color:var(--muted)}.segmented{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.segmented button{border:0;padding:6px 10px;background:transparent;color:var(--muted);cursor:pointer}.segmented button.active{background:#1e4e75;color:white}.price-chart{height:390px}.metric-chart{height:205px}.metric-panel select{border:1px solid var(--line);border-radius:8px;background:#091625;color:var(--text);padding:7px 9px}.chart-footnote{font-size:10px;margin:4px 0 0}.annotation-ledger{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.annotation-ledger span{border:1px solid;border-radius:999px;padding:3px 8px;font-size:10px;color:#cdeef3;background:rgba(8,145,178,.08)}.scenario-panel{overflow:hidden}.scenario-card{border-left:2px solid #3b82b9;padding:9px 0 9px 12px;margin-top:8px;background:linear-gradient(90deg,rgba(43,116,169,.1),transparent);transition:transform .22s ease,background .22s ease,box-shadow .22s ease}.scenario-card.agent-focus{transform:translateX(4px);background:linear-gradient(90deg,rgba(34,211,238,.2),rgba(34,211,238,.02));box-shadow:inset 0 0 0 1px rgba(34,211,238,.2);animation:agent-focus-pulse 1.8s ease-in-out infinite}.scenario-card:nth-of-type(3){border-color:#7c8da3}.scenario-card:nth-of-type(4){border-color:#d06e6e}.scenario-card h4{font-size:13px;margin:0 0 6px}.scenario-card p{font-size:11px;color:#bac8da;line-height:1.55;margin:3px 0}.scenario-card b{color:#edf4fc}.scenario-card.compact{padding-top:7px;padding-bottom:7px}.checkpoint-row{display:flex;gap:4px;flex-wrap:wrap;margin-top:7px}.checkpoint-row span{padding:3px 6px;color:#9cb1c9}.probability-note{font-size:10px;border-top:1px solid var(--line);padding-top:9px}.detail-grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(320px,.7fr);gap:12px;margin-top:12px}.timeline{max-height:330px;overflow:auto}.timeline-item{display:grid;grid-template-columns:82px 1fr;gap:13px;padding:12px 4px;border-top:1px solid var(--line);color:inherit;text-decoration:none}.timeline-item time{font-size:11px;color:#71bdf5}.timeline-item h4{font-size:13px;margin:0 0 4px}.timeline-item p{font-size:11px;color:#b4c2d4;margin:0 0 5px;line-height:1.5}.timeline-item small{color:var(--muted)}.empty-state,.empty-plan{color:var(--muted);padding:30px 5px}.empty-plan strong{color:#f1be6b}.plan-hero{border:1px solid rgba(80,169,255,.35);background:rgba(31,99,151,.13);padding:12px;border-radius:10px}.plan-hero strong{color:#68bafa}.plan-panel dl{display:grid;grid-template-columns:72px 1fr;gap:8px 10px;font-size:11px}.plan-panel dt{color:var(--muted)}.plan-panel dd{margin:0;color:#d7e2ef;word-break:break-word}.flow-summary{border-top:1px solid var(--line);padding-top:12px;margin-top:14px}.flow-summary span{font-size:11px;color:#9fb2c8}.narration-ledger{grid-column:1/-1}.narration-item{padding:10px 2px;border-top:1px solid var(--line)}.narration-item p{margin:4px 0;color:#b9cad8;font-size:12px}.narration-item small{color:var(--muted)}.strategy-fade-enter-active,.strategy-fade-leave-active,.agent-note-enter-active,.agent-note-leave-active{transition:opacity .2s ease,transform .2s ease}.strategy-fade-enter-from,.strategy-fade-leave-to,.agent-note-enter-from,.agent-note-leave-to{opacity:0;transform:translateY(5px)}@keyframes agent-focus-pulse{50%{box-shadow:inset 0 0 0 1px rgba(34,211,238,.42),0 0 20px rgba(34,211,238,.1)}}@media(max-width:1100px){.strategy-tabs{grid-template-columns:repeat(3,1fr)}.workbench-grid,.detail-grid{grid-template-columns:1fr}.decision-column{grid-template-columns:1fr 1fr}.agent-narration{grid-template-columns:1fr}}@media(max-width:700px){.workbench-shell{padding:12px;border-radius:12px}.workbench-header,.strategy-context{align-items:flex-start;flex-direction:column}.strategy-tabs{grid-template-columns:repeat(2,1fr)}.decision-column{grid-template-columns:1fr}.price-chart{height:330px}.health-strip{justify-content:flex-start}.timeline-item{grid-template-columns:68px 1fr}}@media(prefers-reduced-motion:reduce){.strategy-tabs button,.strategy-fade-enter-active,.strategy-fade-leave-active,.agent-note-enter-active,.agent-note-leave-active,.scenario-card.agent-focus{transition:none!important;animation:none!important}}
.strategy-context{flex-wrap:wrap}.risk-strip{width:100%;display:flex;gap:7px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:9px}.risk-strip span{font-size:10px;color:#f2c67d;border:1px solid rgba(242,198,125,.25);border-radius:999px;padding:4px 8px}
.freshness-warning{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:14px 0 4px;padding:10px 12px;border:1px solid rgba(248,113,113,.42);border-radius:10px;background:rgba(127,29,29,.18);color:#fecaca}.freshness-warning strong{font-size:12px}.freshness-warning span{font-size:10px;border-left:1px solid rgba(254,202,202,.25);padding-left:8px}
</style>

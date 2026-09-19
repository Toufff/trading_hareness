<script setup lang="ts">
/**
 * One discipline card: today's action, the K-line with the plan's lines, the line table, sizing,
 * omitted lines, (new buy) the pool's research conditions, and evidence/history.  Read-only.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue';
import ResearchOnlyBadge from '../ResearchOnlyBadge.vue';
import DisciplineChart, { type RightLabel } from './DisciplineChart.vue';
import DisciplineLineTable from './DisciplineLineTable.vue';
import { DEFAULT_LAYERS, buildDailyOption, buildMinuteOption, type ChartLayers } from './discipline-chart-option';
import {
  CHECK_LABEL, KIND_LABEL, LINE_STYLE, STAGE_LABEL, STAGE_TONE, STATUS_LABEL, buildAxis, dateOnly, defaultVisibleKinds,
  distance, effectiveStatus, hardStopPrice, latestLineStates, latestPrice, lineTag, mergeLines, num, price2, priceLines,
  shanghaiStamp, timeStopDeadline, capSummary, todayAction, trailingBelow, validityRemaining, atrOf,
} from './discipline-model';
import { errorText, fetchDailyChart, fetchEvaluations, fetchHistory, fetchMinuteChart, fetchReconciliations } from './discipline-api';
import type { DailyChart, DisciplinePlan, EvaluationsResponse, HistoryResponse, MinuteChart, TradeFill } from './types';

const props = withDefaults(defineProps<{
  plan: DisciplinePlan;
  today: string;
  chart?: DailyChart | null;
  evaluations?: EvaluationsResponse | null;
  trades?: TradeFill[] | null;
  readOnly?: boolean;
  compact?: boolean;
}>(), { chart: null, evaluations: null, trades: null, readOnly: false, compact: false });

const chartData = ref<DailyChart | null>(props.chart);
const chartError = ref('');
const chartLoading = ref(false);
const evalData = ref<EvaluationsResponse | null>(props.evaluations);
const history = ref<HistoryResponse | null>(null);
const historyError = ref('');
const tradeRows = ref<TradeFill[]>(props.trades ?? []);
const mode = ref<'daily' | 'minute'>('daily');
const minuteDate = ref<string>('');
const minute = ref<MinuteChart | null>(null);
const minuteError = ref('');
const minuteLoading = ref(false);
const layers = reactive<ChartLayers>({ ...DEFAULT_LAYERS });
const showAll = ref(false);
const toggled = ref<Set<string>>(new Set());
const highlightKey = ref<string | null>(null);
const focusPrice = ref<number | null>(null);
const oldCard = ref<DisciplinePlan | null>(null);
const section = ref('sizing');

watch(() => props.chart, (value) => { if (value) chartData.value = value; });
watch(() => props.evaluations, (value) => { if (value) evalData.value = value; });
watch(() => props.trades, (value) => { if (value) tradeRows.value = value; });

async function loadChart() {
  chartLoading.value = true;
  chartError.value = '';
  try {
    chartData.value = await fetchDailyChart(props.plan.plan_id);
  } catch (cause) {
    chartError.value = `K 线读取失败：${errorText(cause)}`;
  } finally {
    chartLoading.value = false;
  }
}

async function loadSide() {
  const tasks: Array<Promise<unknown>> = [];
  if (!props.evaluations) tasks.push(fetchEvaluations(props.plan.plan_id).then((value) => { evalData.value = value; }).catch(() => undefined));
  tasks.push(fetchHistory(props.plan.account_key, props.plan.symbol).then((value) => { history.value = value; })
    .catch((cause) => { historyError.value = errorText(cause); }));
  if (!props.trades) {
    const from = new Date(`${props.plan.trading_date}T00:00:00+08:00`);
    from.setDate(from.getDate() - 20);
    tasks.push(fetchReconciliations(props.plan.account_key, { symbol: props.plan.symbol, from: from.toISOString().slice(0, 10) })
      .then((value) => { tradeRows.value = value.trades; }).catch(() => undefined));
  }
  await Promise.all(tasks);
}

onMounted(() => {
  if (!chartData.value) void loadChart();
  void loadSide();
});

const status = computed(() => effectiveStatus(props.plan, props.today));
const action = computed(() => todayAction(props.plan, evalData.value, props.today));
const states = computed(() => latestLineStates(evalData.value));
const current = computed(() => latestPrice(props.plan, chartData.value));
const atr14 = computed(() => atrOf(props.plan, chartData.value));
const hardStop = computed(() => hardStopPrice(props.plan));
const hardGap = computed(() => distance(current.value.price, hardStop.value, atr14.value));
const merged = computed(() => mergeLines(priceLines(props.plan)));
const keyByIndex = computed(() => {
  const map = new Map<number, string>();
  for (const line of merged.value) for (const id of line.ids) map.set(Number(id.split(':')[0]), line.key);
  return map;
});
const keyOf = (index: number) => keyByIndex.value.get(index) ?? null;
const hardStopKey = computed(() => merged.value.find((line) => line.kinds.includes('hard_stop'))?.key ?? null);
const defaultKinds = computed(() => defaultVisibleKinds(props.plan, action.value));
const visibleKeys = computed(() => {
  const keys = new Set<string>();
  for (const line of merged.value) {
    const byDefault = showAll.value || line.kinds.some((kind) => defaultKinds.value.has(kind));
    if (byDefault !== toggled.value.has(line.key)) keys.add(line.key);
  }
  return keys;
});
function toggleLine(key: string) {
  const next = new Set(toggled.value);
  if (next.has(key)) next.delete(key); else next.add(key);
  toggled.value = next;
}
const visibleLines = computed(() => merged.value.filter((line) => visibleKeys.value.has(line.key)));
const axis = computed(() => (chartData.value ? buildAxis(chartData.value) : []));
const deadline = computed(() => {
  const line = props.plan.lines.find((item) => item.kind === 'time_stop');
  const day = timeStopDeadline(props.plan, chartData.value?.sessions ?? []);
  if (!line || !day) return null;
  return { date: day, label: `T+${line.trading_days} 收盘未站回 ${price2(line.price)} → 退出` };
});
const timeStopKey = computed(() => {
  const index = props.plan.lines.findIndex((item) => item.kind === 'time_stop');
  return index >= 0 ? keyOf(index) : null;
});
const trail = computed(() => {
  const index = props.plan.lines.findIndex((item) => item.kind === 'trail');
  const line = props.plan.lines[index];
  const key = index >= 0 ? keyOf(index) : null;
  if (!line || !key || !visibleKeys.value.has(key)) return null;
  const arm = num(line.price);
  const target = num(line.action.value);
  if (arm === null || target === null) return null;
  const fired = evalData.value?.transitions.find((item) => item.kind === 'trail' && item.to === 'triggered');
  return { armPrice: arm, target, triggeredOn: fired?.date ?? null };
});
const buyBand = computed(() => {
  if (props.plan.plan_kind !== 'new_buy') return null;
  const trigger = num(props.plan.lines.find((line) => line.kind === 'trigger')?.price);
  const cap = num(props.plan.lines.find((line) => line.kind === 'chase_cap')?.price);
  return trigger !== null && cap !== null && cap > trigger ? { low: trigger, high: cap } : null;
});
const referencePrice = computed(() => num(props.plan.sizing?.reference_price));
const costPrice = computed(() => (props.plan.plan_kind === 'holding' ? num(props.plan.position?.average_cost) : null));
const symbolTrades = computed(() => tradeRows.value.filter((trade) => trade.symbol === props.plan.symbol));

// A phone keeps the plot readable: narrower tag gutter, tags without the explanatory tail.
const narrow = ref(typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 900px)').matches);
const gutter = computed(() => (narrow.value ? 92 : 196));
const dailyOption = computed(() => (chartData.value ? buildDailyOption({
  gridRight: gutter.value,
  chart: chartData.value, axis: axis.value, lines: visibleLines.value, planDate: props.plan.trading_date,
  validUntil: dateOnly(props.plan.valid_until), highlightKey: highlightKey.value, focusPrice: focusPrice.value,
  layers, deadline: deadline.value,
  confirmKeyEndsAtDeadline: timeStopKey.value, trail: trail.value, buyBand: buyBand.value,
  referencePrice: referencePrice.value, costPrice: costPrice.value, ladder: history.value?.ladder ?? [],
  transitions: evalData.value?.transitions ?? [], trades: symbolTrades.value,
}) : null));

const rightLabels = computed<RightLabel[]>(() => {
  const labels: RightLabel[] = visibleLines.value.map((line) => ({
    key: line.key, price: line.price, text: narrow.value ? line.text : lineTag(line), color: LINE_STYLE[line.style].color,
  }));
  if (referencePrice.value !== null) labels.push({ key: 'reference', price: referencePrice.value, text: `参考 ${price2(referencePrice.value)}`, color: LINE_STYLE.reference.color });
  if (costPrice.value !== null) labels.push({ key: 'cost', price: costPrice.value, text: `成本 ${price2(costPrice.value)}`, color: LINE_STYLE.cost.color });
  if (current.value.price !== null) labels.push({ key: 'current', price: current.value.price, text: narrow.value ? `现价 ${price2(current.value.price)}` : `现价 ${price2(current.value.price)}（${current.value.date.slice(5)}收）`, color: '#1f2937', current: true });
  return labels;
});

const minuteSessions = computed(() => {
  const bars = chartData.value?.bars ?? [];
  return bars.map((bar) => bar.date).filter((day) => day >= props.plan.trading_date).reverse();
});
async function loadMinute() {
  minuteLoading.value = true;
  minuteError.value = '';
  try {
    minute.value = await fetchMinuteChart(props.plan.plan_id, minuteDate.value || null);
    if (!minuteDate.value && minute.value) minuteDate.value = minute.value.date;
  } catch (cause) {
    minuteError.value = `分钟线读取失败：${errorText(cause)}`;
  } finally {
    minuteLoading.value = false;
  }
}
watch(mode, (value) => { if (value === 'minute' && !minute.value) void loadMinute(); });
watch(minuteDate, (value, previous) => { if (previous && value !== previous && mode.value === 'minute') void loadMinute(); });
const minuteOption = computed(() => (minute.value ? buildMinuteOption({ rows: minute.value.rows, hardStop: hardStop.value, closeOnly: minute.value.bar_type === 'close_only' }) : null));
const minuteStreak = computed(() => trailingBelow((minute.value?.rows ?? []).map((row) => row.close), hardStop.value));
const intradayBars = computed(() => props.plan.lines.find((line) => line.kind === 'hard_stop' && line.confirm?.basis === 'minute')?.confirm?.bars ?? 3);

function onFocus(payload: { key: string; price: number }) {
  highlightKey.value = payload.key;
  focusPrice.value = focusPrice.value === payload.price ? null : payload.price;
  if (!visibleKeys.value.has(payload.key)) toggleLine(payload.key);
}
function onChartClick(key: string) {
  const line = merged.value.find((item) => item.key === key);
  if (line) onFocus({ key, price: line.price });
}

// ---- sizing / evidence
const sizing = computed(() => props.plan.sizing ?? null);
const caps = computed(() => capSummary(props.plan.sizing ?? null));
const overCap = computed(() => (sizing.value ? (num(sizing.value.current_exposure_pct) ?? 0) > (num(sizing.value.target_exposure_pct) ?? 0) : false));
const maxSharesFormula = computed(() => {
  const s = sizing.value;
  if (!s) return '';
  return `floor(${s.equity} × ${s.risk_per_trade_pct}% ÷ (${price2(s.reference_price)} − ${price2(s.hard_stop)}) ÷ 100) × 100 = ${s.max_shares} 股`;
});
const stopPct = computed(() => {
  const s = sizing.value;
  const ref = num(s?.reference_price);
  const gap = num(s?.stop_distance);
  return ref && gap !== null ? `${(gap / ref * 100).toFixed(2)}%` : '—';
});
const t1Locked = computed(() => Number(props.plan.metrics?.t1_locked_shares ?? 0));
const entry = computed(() => props.plan.metrics?.entry ?? null);
const research = computed(() => props.plan.metrics?.recommendation_conditions ?? null);
const omitted = computed(() => props.plan.metrics?.omitted_lines ?? []);
const failedChecks = computed(() => (props.plan.quality ?? []).filter((check) => !check.passed));
const remaining = computed(() => validityRemaining(props.plan, props.today));
const chain = computed(() => [...(history.value?.items ?? [])].sort((a, b) => b.as_of_at.localeCompare(a.as_of_at)));
</script>

<template>
  <article
    class="discipline-detail"
    :class="{ rejected: plan.status === 'rejected_by_quality', compact }"
    :data-plan-id="plan.plan_id"
    data-testid="discipline-detail"
  >
    <header class="detail-head">
      <div class="title">
        <h3>{{ plan.name }}<small>（{{ plan.symbol }}）</small></h3>
        <span
          class="stage-chip"
          :class="`tone-${STAGE_TONE[plan.stage] ?? 'neutral'}`"
        >{{ STAGE_LABEL[plan.stage] ?? plan.stage }}</span>
        <span
          class="status-chip"
          :class="`status-${status}`"
        >{{ STATUS_LABEL[status] ?? status }}</span>
        <span
          v-if="plan.plan_kind === 'new_buy'"
          class="kind-chip"
        >新买计划</span>
      </div>
      <div class="meta">
        计划日 {{ plan.trading_date }} · 有效至 {{ dateOnly(plan.valid_until) }}（剩 {{ remaining }} 个交易日） · 生成 {{ shanghaiStamp(plan.as_of_at) }}
      </div>
    </header>

    <el-alert
      v-if="plan.status === 'rejected_by_quality'"
      type="error"
      :closable="false"
      show-icon
      :title="`不可用：质量门未通过 ${failedChecks.length} 项（${failedChecks.map((check) => CHECK_LABEL[check.check_id] ?? check.check_id).join('、')}）。此卡仅供查看，不据此操作。`"
    />

    <section
      class="today"
      :class="`tone-${action.tone}`"
      data-testid="today-action"
    >
      <span class="today-label">今日动作</span>
      <strong>{{ action.headline }}</strong>
      <span class="today-timing">执行时点：{{ action.timing }}</span>
      <span class="today-stop">硬止损 {{ price2(hardStop) }} · 距现价 {{ hardGap?.text ?? '—' }}</span>
      <span
        v-if="caps"
        class="today-stop"
        data-testid="today-caps"
      >仓位：{{ caps.binding }}</span>
    </section>

    <section class="chart-block">
      <div class="chart-toolbar">
        <el-radio-group
          v-model="mode"
          size="small"
          aria-label="K线周期"
        >
          <el-radio-button value="daily">
            日线
          </el-radio-button>
          <el-radio-button value="minute">
            分钟
          </el-radio-button>
        </el-radio-group>
        <template v-if="mode === 'daily'">
          <el-checkbox
            v-model="showAll"
            size="small"
          >
            全部纪律线
          </el-checkbox>
          <el-checkbox
            v-model="layers.ma"
            size="small"
          >
            均线（状态，非止损）
          </el-checkbox>
          <el-checkbox
            v-model="layers.atr"
            size="small"
          >
            ATR 带
          </el-checkbox>
          <el-checkbox
            v-model="layers.ladder"
            size="small"
          >
            止损阶梯
          </el-checkbox>
          <el-checkbox
            v-model="layers.evaluations"
            size="small"
          >
            评估触发
          </el-checkbox>
          <el-checkbox
            v-model="layers.trades"
            size="small"
          >
            成交与对账
          </el-checkbox>
          <el-button
            v-if="focusPrice !== null"
            size="small"
            text
            @click="focusPrice = null; highlightKey = null"
          >
            还原缩放
          </el-button>
        </template>
        <template v-else>
          <el-select
            v-model="minuteDate"
            size="small"
            class="minute-date"
            placeholder="交易日"
            aria-label="分钟线交易日"
          >
            <el-option
              v-for="day in minuteSessions"
              :key="day"
              :label="day"
              :value="day"
            />
          </el-select>
          <span
            v-if="minute?.rows.length"
            class="streak"
            :class="{ danger: minuteStreak > 0 }"
            data-testid="minute-streak"
          >
            硬止损盘中版：已连续 {{ Math.min(minuteStreak, intradayBars) }}/{{ intradayBars }} 根收于止损下
          </span>
        </template>
      </div>

      <template v-if="mode === 'daily'">
        <el-skeleton
          v-if="chartLoading && !chartData"
          :rows="8"
          animated
        />
        <el-alert
          v-else-if="chartError"
          type="error"
          :closable="false"
          show-icon
          :title="chartError"
        >
          <el-button
            size="small"
            @click="loadChart"
          >
            重试
          </el-button>
        </el-alert>
        <template v-else-if="chartData && dailyOption">
          <el-alert
            v-if="chartData.price_basis.warning"
            type="warning"
            :closable="false"
            show-icon
            :title="chartData.price_basis.warning"
          />
          <DisciplineChart
            :option="dailyOption"
            :labels="rightLabels"
            :gutter="gutter"
            :hard-stop-key="hardStopKey"
            :hard-stop-terms="chartData.hard_stop_terms"
            :active-key="highlightKey"
            :height="compact ? 400 : 470"
            @hover-line="highlightKey = $event"
            @click-line="onChartClick"
          />
          <ul
            class="marker-legend"
            data-testid="marker-legend"
            aria-label="图例"
          >
            <li v-if="layers.trades && symbolTrades.length">
              <i class="m-buy">▲B</i><i class="m-sell">▼S</i>真实成交（×N 为当日笔数，悬停看每笔价格、股数与对账结论）
            </li>
            <li v-if="chartData.structure_points.length">
              <i class="m-struct">▲</i>结构点（如 low20）
            </li>
            <li v-if="layers.evaluations">
              <i class="m-eval">▼▲</i>评估触发（线由等待→触发的那天）
            </li>
            <li v-if="layers.ladder">
              <i class="m-ladder">◆</i>止损阶梯下移（附理由）
            </li>
            <li v-if="chartData.closures.length">
              <i class="m-closed">▇</i>休市
            </li>
            <li v-if="deadline">
              <i class="m-deadline">┆</i>时间止损截止日
            </li>
            <li v-if="buyBand">
              <i class="m-band">▇</i>可买区间（触发下沿–追高上限）
            </li>
          </ul>
          <p class="basis-note">
            口径：{{ chartData.price_basis.note }}<template v-if="chartData.price_basis.corporate_actions.length">
              窗口内除权/除息日：{{ chartData.price_basis.corporate_actions.map((item) => item.date).join('、') }}（未复权显示）。
            </template>
          </p>
        </template>
      </template>
      <template v-else>
        <el-skeleton
          v-if="minuteLoading && !minute"
          :rows="6"
          animated
        />
        <el-alert
          v-else-if="minuteError"
          type="error"
          :closable="false"
          show-icon
          :title="minuteError"
        >
          <el-button
            size="small"
            @click="loadMinute"
          >
            重试
          </el-button>
        </el-alert>
        <el-empty
          v-else-if="minute && !minute.rows.length"
          :description="minute.reason || '没有分钟线'"
          :image-size="48"
          data-testid="minute-empty"
        />
        <DisciplineChart
          v-else-if="minuteOption"
          :option="minuteOption"
          :labels="[]"
          :gutter="110"
          :height="compact ? 360 : 420"
        />
        <p
          v-if="minute?.rows.length"
          class="basis-note"
        >
          来源：{{ minute.source }} · {{ minute.date }} · {{ minute.count }} 根 1 分钟{{ minute.bar_type === 'close_only' ? '收盘价（longhu 分时只有每分钟一个价格，按分时线显示）' : ' K' }}，橙线为 VWAP
        </p>
      </template>
    </section>

    <DisciplineLineTable
      :plan="plan"
      :price="current.price"
      :atr14="atr14"
      :states="states"
      :key-of="keyOf"
      :highlight-key="highlightKey"
      :visible-keys="visibleKeys"
      @hover="highlightKey = $event"
      @focus="onFocus"
      @toggle="toggleLine"
    />

    <el-tabs
      v-model="section"
      class="detail-tabs"
    >
      <el-tab-pane
        label="仓位推导"
        name="sizing"
      >
        <dl
          v-if="sizing"
          class="sizing-grid"
          data-testid="sizing"
        >
          <div><dt>账户权益</dt><dd>{{ sizing.equity }}</dd></div>
          <div><dt>单笔风险预算</dt><dd>{{ sizing.risk_per_trade_pct }}% = {{ sizing.risk_amount }}</dd></div>
          <div><dt>止损距离</dt><dd>{{ price2(sizing.stop_distance) }}（{{ stopPct }}，参考 {{ price2(sizing.reference_price) }} → 硬止损 {{ price2(sizing.hard_stop) }}）</dd></div>
          <div class="wide">
            <dt>max_shares</dt><dd><code>{{ maxSharesFormula }}</code></dd>
          </div>
          <div data-testid="cap-basis">
            <dt>阶段上限（数据校准）</dt><dd>{{ caps?.cap }}<small v-if="sizing.exposure_basis?.fallback">（fallback）</small></dd>
          </div>
          <div class="wide">
            <dt>上限依据</dt><dd>{{ caps?.basis }}</dd>
          </div>
          <div
            class="wide"
            data-testid="binding-constraint"
          >
            <dt>起约束的限制</dt><dd :class="{ binding: true }">{{ caps?.binding }}</dd>
          </div>
          <div><dt>建议股数</dt><dd>{{ sizing.recommended_shares }} 股</dd></div>
          <div><dt>当前持仓 / 可卖</dt><dd>{{ plan.position?.quantity ?? 0 }} / {{ plan.position?.sellable_quantity ?? 0 }} 股<small v-if="t1Locked > 0">（生成日 T+1 锁定 {{ t1Locked }} 股，下一交易日可卖）</small></dd></div>
          <div>
            <dt>当前仓位</dt><dd :class="{ over: overCap }">
              {{ sizing.current_exposure_pct }}%<small v-if="overCap">（超出阶段上限）</small>
            </dd>
          </div>
          <div>
            <dt>当前风险比例</dt><dd :class="{ over: (num(sizing.current_risk_pct) ?? 0) > (num(sizing.risk_per_trade_pct) ?? 0) }">
              {{ sizing.current_risk_pct ?? '—' }}%
            </dd>
          </div>
          <div
            v-if="entry"
            class="wide"
          >
            <dt>入场参考价</dt><dd>{{ price2(entry.entry_price) }} = max(lane 结构参考 {{ price2(entry.lane_reference) }}，{{ entry.last_close_date }} 收盘 {{ price2(entry.last_close) }})；止损、股数、移动止损均按它计算</dd>
          </div>
        </dl>
        <el-empty
          v-else
          description="本计划没有仓位推导"
          :image-size="40"
        />
      </el-tab-pane>
      <el-tab-pane
        :label="`未生成的线（${omitted.length}）`"
        name="omitted"
      >
        <ul
          v-if="omitted.length"
          class="plain-list"
        >
          <li
            v-for="item in omitted"
            :key="item.kind + item.reason"
          >
            <strong>{{ KIND_LABEL[item.kind] ?? item.kind }}</strong>：{{ item.reason }}
          </li>
        </ul>
        <p
          v-else
          class="muted"
        >
          模板中的每条可选线都已生成。
        </p>
      </el-tab-pane>
      <el-tab-pane
        v-if="plan.plan_kind === 'new_buy'"
        label="推荐池研究条件"
        name="research"
      >
        <div
          v-if="research"
          class="research"
          data-testid="research-conditions"
        >
          <span class="research-badge">{{ research.note || '研究条件，非系统线' }}</span>
          <p v-if="research.trigger">
            <strong>观察触发：</strong>{{ research.trigger }}
          </p>
          <p v-if="research.invalidation">
            <strong>取消条件：</strong>{{ research.invalidation }}
          </p>
          <p v-if="research.why_now">
            <strong>为什么是现在：</strong>{{ research.why_now }}
          </p>
          <p class="muted">
            决策 {{ (research.decision_id || '').slice(0, 12) }} · {{ research.as_of_date }}；这些文字不参与系统评估。
          </p>
        </div>
        <p
          v-else
          class="muted"
        >
          生成本卡时推荐池没有该股的人读条件。
        </p>
      </el-tab-pane>
      <el-tab-pane
        label="证据与历史"
        name="evidence"
      >
        <dl class="evidence">
          <div><dt>plan_key</dt><dd><code>{{ plan.plan_key }}</code></dd></div>
          <div><dt>生成时间</dt><dd>{{ shanghaiStamp(plan.as_of_at) }}（{{ plan.generator_version }}）</dd></div>
          <div><dt>日线口径</dt><dd>{{ chartData?.price_basis.note ?? '未复权 canonical 日线' }}</dd></div>
          <div><dt>持仓快照</dt><dd>{{ plan.position ? shanghaiStamp(plan.position.observed_at) : '空仓（新买计划）' }}</dd></div>
        </dl>
        <h4>替代链</h4>
        <p
          v-if="historyError"
          class="muted"
        >
          历史读取失败：{{ historyError }}
        </p>
        <ul
          v-else
          class="chain"
        >
          <li
            v-for="item in chain"
            :key="item.plan_id"
            :class="{ self: item.plan_id === plan.plan_id }"
          >
            <span>{{ shanghaiStamp(item.as_of_at) }}</span>
            <span
              class="status-chip"
              :class="`status-${item.status}`"
            >{{ STATUS_LABEL[item.status] ?? item.status }}</span>
            <span>硬止损 {{ price2(item.sizing?.hard_stop) }}</span>
            <el-button
              v-if="item.plan_id !== plan.plan_id"
              size="small"
              text
              @click="oldCard = item"
            >
              查看旧卡
            </el-button>
            <span
              v-else
              class="muted"
            >当前卡</span>
          </li>
        </ul>
        <h4>质量门</h4>
        <ul
          class="checks"
          data-testid="quality-checks"
        >
          <li
            v-for="check in plan.quality ?? []"
            :key="check.check_id"
            :class="{ failed: !check.passed }"
          >
            <span>{{ check.passed ? '通过' : '未通过' }}</span><strong>{{ CHECK_LABEL[check.check_id] ?? check.check_id }}</strong><small>{{ check.detail }}</small>
          </li>
        </ul>
        <details class="refs">
          <summary>证据引用（{{ plan.evidence_refs?.length ?? 0 }}）</summary><code
            v-for="evidenceRef in plan.evidence_refs ?? []"
            :key="evidenceRef"
          >{{ evidenceRef }}</code>
        </details>
      </el-tab-pane>
    </el-tabs>

    <footer class="detail-foot">
      <ResearchOnlyBadge /><span>研究用途，系统不下单</span>
    </footer>

    <el-dialog
      v-if="!readOnly"
      :model-value="Boolean(oldCard)"
      width="min(1100px, 96vw)"
      title="旧纪律卡（只读）"
      destroy-on-close
      @close="oldCard = null"
    >
      <DisciplineDetail
        v-if="oldCard"
        :plan="oldCard"
        :today="today"
        read-only
        compact
      />
    </el-dialog>
  </article>
</template>

<style scoped>
.discipline-detail { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.detail-head .title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.detail-head h3 { margin: 0; font-size: 18px; }
.detail-head h3 small { color: var(--el-text-color-secondary); font-weight: 400; font-size: 13px; }
.detail-head .meta { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
.stage-chip, .status-chip, .kind-chip { padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.tone-crash { background: #fee2e2; color: #991b1b; }
.tone-broken { background: #1f2937; color: #f9fafb; }
.tone-breakout { background: #dcfce7; color: #166534; }
.tone-trend { background: #dbeafe; color: #1e40af; }
.tone-pullback { background: #e0e7ff; color: #3730a3; }
.tone-base { background: #fef3c7; color: #92400e; }
.tone-neutral { background: #f1f5f9; color: #475569; }
.status-active { background: #ecfdf5; color: #047857; }
.status-rejected_by_quality { background: #fee2e2; color: #b91c1c; }
.status-superseded, .status-expired { background: #f1f5f9; color: #64748b; }
.kind-chip { background: #eff6ff; color: #1d4ed8; }
.today { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; padding: 14px 16px; border-radius: 10px; border-left: 5px solid; background: var(--el-fill-color-light); }
.today strong { font-size: 20px; line-height: 1.4; }
.today-label { grid-row: span 4; align-self: center; font-size: 12px; color: var(--el-text-color-secondary); writing-mode: horizontal-tb; }
.today-timing, .today-stop { color: var(--el-text-color-regular); font-size: 13px; }
.today.tone-danger { border-color: #d93026; background: #fef2f2; }
.today.tone-warning { border-color: #f08c00; background: #fff7ed; }
.today.tone-success { border-color: #1a9e5a; background: #f0fdf4; }
.today.tone-info { border-color: #3b82f6; background: #eff6ff; }
.chart-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; margin-bottom: 6px; }
.minute-date { width: 140px; }
.streak { font-size: 13px; color: var(--el-text-color-regular); }
.streak.danger { color: #b91c1c; font-weight: 600; }
.marker-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 4px 0 0; padding: 0; list-style: none; font-size: 12px; color: var(--el-text-color-regular); }
.marker-legend i { margin-right: 3px; font-style: normal; font-weight: 700; }
.m-buy { color: #dc2626; } .m-sell { color: #15803d; margin-left: 2px; } .m-struct { color: #475569; } .m-eval { color: #d93026; }
.m-ladder { color: #eab308; } .m-closed { color: #cbd5e1; } .m-deadline { color: #8a94a6; } .m-band { color: #bfe8d1; }
.basis-note { margin: 4px 0 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.sizing-grid, .evidence { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 16px; margin: 0; }
.sizing-grid .wide, .evidence div { grid-column: span 1; }
.sizing-grid .wide { grid-column: 1 / -1; }
.sizing-grid dt, .evidence dt { color: var(--el-text-color-secondary); font-size: 12px; }
.sizing-grid dd, .evidence dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.sizing-grid dd.over { color: #b91c1c; font-weight: 600; }
.sizing-grid dd.binding { font-weight: 600; }
.sizing-grid small { color: var(--el-text-color-secondary); }
.plain-list { margin: 0; padding-left: 18px; line-height: 1.7; }
.muted { color: var(--el-text-color-secondary); }
.research { padding: 10px 12px; border: 1px dashed #93c5fd; border-radius: 8px; background: #f8fbff; }
.research-badge { display: inline-block; margin-bottom: 6px; padding: 2px 8px; border-radius: 4px; background: #dbeafe; color: #1e40af; font-size: 12px; }
.research p { margin: 4px 0; line-height: 1.6; }
.chain { margin: 0; padding: 0; list-style: none; }
.chain li { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 4px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.chain li.self { font-weight: 600; }
.checks { margin: 0; padding: 0; list-style: none; font-size: 13px; }
.checks li { display: grid; grid-template-columns: 52px 240px minmax(0, 1fr); gap: 8px; padding: 3px 0; }
.checks li.failed { color: #b91c1c; font-weight: 600; }
.checks small { color: var(--el-text-color-secondary); overflow-wrap: anywhere; }
.checks li.failed small { color: #b91c1c; }
.refs code { display: block; font-size: 11px; overflow-wrap: anywhere; }
h4 { margin: 14px 0 6px; font-size: 14px; }
.detail-foot { position: sticky; bottom: 0; display: flex; align-items: center; gap: 8px; padding: 8px 0 2px; background: var(--el-bg-color); color: var(--el-text-color-secondary); font-size: 12px; }
.discipline-detail.rejected { outline: 2px solid #fca5a5; outline-offset: 6px; border-radius: 8px; }
@media (max-width: 900px) {
  .sizing-grid, .evidence { grid-template-columns: 1fr; }
  .checks li { grid-template-columns: 52px minmax(0, 1fr); }
  .checks small { grid-column: 1 / -1; }
  .today strong { font-size: 17px; }
}
</style>

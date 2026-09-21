<script setup lang="ts">
/**
 * The holdings page's discipline block: today's action summary first, then the list of cards with the
 * selected card's detail.  Everything is read from the owner API through the adapter; nothing is written.
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import DisciplineDetail from './DisciplineDetail.vue';
import { ALL_STATUSES, DEFAULT_STATUSES, errorText, fetchDailyChart, fetchEvaluations, fetchLatestPlans, fetchReconciliations } from './discipline-api';
import {
  STAGE_LABEL, STAGE_TONE, STATUS_LABEL, atrOf, distance, effectiveStatus, hardStopPrice, latestPrice, nearestPendingLine,
  price2, shanghaiStamp, shanghaiToday, snapshotWarnings, todayAction, totalRiskPct, triggeredCount, validityRemaining, KIND_LABEL,
} from './discipline-model';
import type { DailyChart, DisciplinePlan, EvaluationsResponse, TradeFill } from './types';

const props = defineProps<{ accountKey: string; snapshotObservedAt?: string | null; now?: Date }>();

const plans = ref<DisciplinePlan[]>([]);
const charts = ref<Record<string, DailyChart | null>>({});
const evaluations = ref<Record<string, EvaluationsResponse | null>>({});
const trades = ref<TradeFill[]>([]);
const loading = ref(false);
const error = ref('');
const sideErrors = ref<string[]>([]);
const showInactive = ref(false);
const selectedId = ref<string | null>(null);
const detailRef = ref<HTMLElement | null>(null);

const today = computed(() => shanghaiToday(props.now ?? new Date()));

async function load() {
  loading.value = true;
  error.value = '';
  sideErrors.value = [];
  try {
    const response = await fetchLatestPlans(props.accountKey, showInactive.value ? ALL_STATUSES : DEFAULT_STATUSES);
    const holding = response.items.filter((plan) => plan.plan_kind === 'holding');
    plans.value = holding;
    if (!holding.some((plan) => plan.plan_id === selectedId.value)) selectedId.value = visiblePlans.value[0]?.plan_id ?? null;
    const earliest = holding.map((plan) => plan.trading_date).sort()[0];
    const [chartResults, evaluationResults, reconciliation] = await Promise.all([
      Promise.allSettled(holding.map((plan) => fetchDailyChart(plan.plan_id))),
      Promise.allSettled(holding.map((plan) => fetchEvaluations(plan.plan_id))),
      earliest ? fetchReconciliations(props.accountKey, { from: earliest }).then((value) => value.trades).catch((cause) => {
        sideErrors.value.push(`成交读取失败：${errorText(cause)}`);
        return [] as TradeFill[];
      }) : Promise.resolve([] as TradeFill[]),
    ]);
    const nextCharts: Record<string, DailyChart | null> = {};
    const nextEvaluations: Record<string, EvaluationsResponse | null> = {};
    holding.forEach((plan, index) => {
      const chart = chartResults[index];
      const evaluation = evaluationResults[index];
      nextCharts[plan.plan_id] = chart?.status === 'fulfilled' ? chart.value : null;
      nextEvaluations[plan.plan_id] = evaluation?.status === 'fulfilled' ? evaluation.value : null;
      if (chart?.status === 'rejected') sideErrors.value.push(`${plan.name} K 线读取失败：${errorText(chart.reason)}`);
    });
    charts.value = nextCharts;
    evaluations.value = nextEvaluations;
    trades.value = reconciliation;
  } catch (cause) {
    error.value = `纪律卡读取失败：${errorText(cause)}`;
    plans.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(() => props.accountKey, load);
watch(showInactive, load);

const visiblePlans = computed(() => plans.value
  .filter((plan) => showInactive.value || effectiveStatus(plan, today.value) !== 'expired')
  .sort((a, b) => a.symbol.localeCompare(b.symbol)));
const selected = computed(() => visiblePlans.value.find((plan) => plan.plan_id === selectedId.value) ?? visiblePlans.value[0] ?? null);
const hiddenCount = computed(() => plans.value.length - visiblePlans.value.length);

const rows = computed(() => visiblePlans.value.map((plan) => {
  const chart = charts.value[plan.plan_id] ?? null;
  const evaluation = evaluations.value[plan.plan_id] ?? null;
  const current = latestPrice(plan, chart);
  const stop = hardStopPrice(plan);
  const gap = distance(current.price, stop, atrOf(plan, chart));
  const nearest = nearestPendingLine(plan, current.price, evaluation);
  return {
    plan, action: todayAction(plan, evaluation, today.value), stop, gap, current, nearest,
    status: effectiveStatus(plan, today.value), remaining: validityRemaining(plan, today.value),
    triggered: triggeredCount(evaluation),
    nearestText: nearest ? `${KIND_LABEL[nearest.kind] ?? nearest.kind} ${price2(nearest.price)}（${distance(current.price, nearest.price, atrOf(plan, chart))?.text ?? '—'}）` : '—',
  };
}));

const totalRisk = computed(() => totalRiskPct(visiblePlans.value));
const snapshotAt = computed(() => props.snapshotObservedAt ?? visiblePlans.value.map((plan) => plan.position?.observed_at).filter(Boolean).sort().reverse()[0] ?? null);
const allSessions = computed(() => [...new Set(Object.values(charts.value).flatMap((chart) => chart?.sessions ?? []))]);
const warnings = computed(() => snapshotWarnings(snapshotAt.value, allSessions.value, trades.value, props.now ?? new Date()));

async function select(planId: string) {
  selectedId.value = planId;
  await nextTick();
  detailRef.value?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
}
</script>

<template>
  <section
    class="discipline-board"
    aria-label="纪律卡"
    data-testid="discipline-board"
  >
    <header class="board-head">
      <div>
        <h3>纪律卡</h3>
        <small>每只持仓今天该做什么、线在 K 线上的位置、离触发还有多远；只读，系统不下单。</small>
      </div>
      <el-space wrap>
        <el-checkbox
          v-model="showInactive"
          size="small"
        >
          显示已过期/已替代
        </el-checkbox>
        <el-button
          size="small"
          :loading="loading"
          @click="load"
        >
          刷新纪律卡
        </el-button>
      </el-space>
    </header>

    <el-skeleton
      v-if="loading && !plans.length"
      :rows="6"
      animated
      data-testid="discipline-loading"
    />
    <el-alert
      v-else-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      data-testid="discipline-error"
    >
      <el-button
        size="small"
        @click="load"
      >
        重试
      </el-button>
    </el-alert>
    <el-empty
      v-else-if="!visiblePlans.length"
      :image-size="60"
      data-testid="discipline-empty"
      :description="hiddenCount ? `现有 ${hiddenCount} 张纪律卡均已过期，可勾选“显示已过期/已替代”查看；新卡请运行 stock-discipline 生成` : '尚未生成纪律卡，运行 stock-discipline 生成'"
    />

    <template v-else>
      <div
        class="summary"
        data-testid="discipline-summary"
      >
        <div class="summary-head">
          <strong>今日动作汇总</strong>
          <span>当前总风险 <b>{{ totalRisk.toFixed(2) }}%</b>（Σ持仓×止损距离/权益，按各卡生成时参考价）</span>
          <span>持仓快照 {{ shanghaiStamp(snapshotAt) }}</span>
          <el-tag
            v-if="warnings.length"
            type="warning"
            effect="dark"
            size="small"
            data-testid="snapshot-warning"
          >
            持仓可能已变：{{ warnings.join('；') }}
          </el-tag>
        </div>
        <div class="summary-rows">
          <button
            v-for="row in rows"
            :key="row.plan.plan_id"
            type="button"
            class="summary-row"
            :class="[`tone-${row.action.tone}`, { active: selected?.plan_id === row.plan.plan_id }]"
            :data-symbol="row.plan.symbol"
            @click="select(row.plan.plan_id)"
          >
            <span class="name">{{ row.plan.name }}<small>（{{ row.plan.symbol }}）</small></span>
            <span
              class="stage-chip"
              :class="`tone-${STAGE_TONE[row.plan.stage] ?? 'neutral'}`"
            >{{ STAGE_LABEL[row.plan.stage] ?? row.plan.stage }}</span>
            <span class="action">{{ row.action.headline }}</span>
            <span class="stop">硬止损 {{ price2(row.stop) }} · {{ row.gap?.text ?? '—' }}</span>
            <span class="nearest">最近待触发：{{ row.nearestText }}</span>
          </button>
        </div>
        <p
          v-for="item in sideErrors"
          :key="item"
          class="side-error"
        >
          {{ item }}
        </p>
      </div>

      <div class="board-body">
        <nav
          class="card-list"
          aria-label="纪律卡列表"
        >
          <button
            v-for="row in rows"
            :key="row.plan.plan_id"
            type="button"
            class="list-item"
            :class="{ active: selected?.plan_id === row.plan.plan_id, rejected: row.plan.status === 'rejected_by_quality' }"
            :data-symbol="row.plan.symbol"
            @click="select(row.plan.plan_id)"
          >
            <div class="item-top">
              <strong>{{ row.plan.name }}</strong><small>{{ row.plan.symbol }}</small>
              <span
                v-if="row.triggered"
                class="dot"
                :title="`${row.triggered} 条线已触发`"
              >{{ row.triggered }}</span>
            </div>
            <div class="item-tags">
              <span
                class="stage-chip"
                :class="`tone-${STAGE_TONE[row.plan.stage] ?? 'neutral'}`"
              >{{ STAGE_LABEL[row.plan.stage] ?? row.plan.stage }}</span>
              <span
                class="status"
                :class="`status-${row.status}`"
              >{{ row.plan.status === 'rejected_by_quality' ? '不可用' : STATUS_LABEL[row.status] }}</span>
            </div>
            <div class="item-facts">
              <span>{{ row.action.short }}</span>
              <span>距硬止损 {{ row.gap ? `${row.gap.pct.toFixed(1)}%` : '—' }}</span>
              <span>剩 {{ row.remaining }} 个交易日</span>
            </div>
          </button>
        </nav>
        <div
          ref="detailRef"
          class="card-detail"
        >
          <DisciplineDetail
            v-if="selected"
            :key="selected.plan_id"
            :plan="selected"
            :today="today"
            :chart="charts[selected.plan_id] ?? null"
            :evaluations="evaluations[selected.plan_id] ?? null"
            :trades="trades"
          />
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.discipline-board { display: flex; flex-direction: column; gap: 12px; padding: 16px; border: 1px solid var(--el-border-color-lighter); border-radius: 10px; background: var(--el-bg-color); }
.board-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.board-head h3 { margin: 0; font-size: 18px; }
.board-head small { color: var(--el-text-color-secondary); }
.summary { padding: 12px; border-radius: 10px; background: var(--el-fill-color-light); }
.summary-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 8px; font-size: 13px; }
.summary-head strong { font-size: 15px; }
.summary-rows { display: flex; flex-direction: column; gap: 6px; }
.summary-row { display: grid; grid-template-columns: 170px 78px minmax(0, 2.4fr) minmax(0, 1.4fr) minmax(0, 1.5fr); align-items: center; gap: 10px;
  width: 100%; padding: 8px 10px; border: 1px solid var(--el-border-color-lighter); border-left: 4px solid; border-radius: 8px; background: var(--el-bg-color);
  text-align: left; font: inherit; font-size: 13px; cursor: pointer; }
.summary-row.active { box-shadow: 0 0 0 2px var(--el-color-primary-light-5); }
.summary-row.tone-danger { border-left-color: #d93026; }
.summary-row.tone-warning { border-left-color: #f08c00; }
.summary-row.tone-success { border-left-color: #1a9e5a; }
.summary-row.tone-info { border-left-color: #3b82f6; }
.summary-row .name small { color: var(--el-text-color-secondary); }
.summary-row .action { font-weight: 600; }
.summary-row .stop { color: #b91c1c; }
.summary-row .nearest { color: var(--el-text-color-secondary); }
.side-error { margin: 6px 0 0; color: #b91c1c; font-size: 12px; }
.board-body { display: grid; grid-template-columns: 270px minmax(0, 1fr); gap: 14px; align-items: start; }
.card-list { display: flex; flex-direction: column; gap: 8px; position: sticky; top: 8px; }
.list-item { display: flex; flex-direction: column; gap: 5px; padding: 10px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px;
  background: var(--el-bg-color); text-align: left; font: inherit; cursor: pointer; }
.list-item.active { border-color: var(--el-color-primary); box-shadow: 0 0 0 1px var(--el-color-primary-light-5); }
.list-item.rejected { border: 2px solid #ef4444; }
.item-top { display: flex; align-items: baseline; gap: 6px; }
.item-top small { color: var(--el-text-color-secondary); }
.dot { margin-left: auto; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px; background: #ef4444; color: var(--gs-paper); font-size: 11px; line-height: 18px; text-align: center; }
.item-tags, .item-facts { display: flex; flex-wrap: wrap; gap: 6px; font-size: 12px; }
.item-facts { color: var(--el-text-color-regular); }
.stage-chip, .status { padding: 1px 7px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.tone-crash { background: #fee2e2; color: #991b1b; }
.tone-broken { background: #1f2937; color: #f9fafb; }
.tone-breakout { background: #dcfce7; color: #166534; }
.tone-trend { background: #dbeafe; color: #1e40af; }
.tone-pullback { background: #e0e7ff; color: #3730a3; }
.tone-base { background: #fef3c7; color: #92400e; }
.tone-neutral { background: #f1f5f9; color: #475569; }
.status-active { background: #ecfdf5; color: #047857; }
.status-rejected_by_quality { background: #fee2e2; color: #b91c1c; font-weight: 600; }
.status-superseded, .status-expired { background: #f1f5f9; color: #64748b; }
.card-detail { min-width: 0; scroll-margin-top: 12px; }
@media (max-width: 900px) {
  .discipline-board { padding: 10px; }
  .board-head { flex-direction: column; }
  .summary-row { grid-template-columns: 1fr auto; }
  .summary-row .action, .summary-row .stop, .summary-row .nearest { grid-column: 1 / -1; }
  .board-body { grid-template-columns: 1fr; }
  .card-list { position: static; flex-direction: row; overflow-x: auto; }
  .list-item { min-width: 190px; }
}
</style>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { usePolling } from '../composables/usePolling';
import type { WorkbenchAnnotation } from './stock-workbench-control';
import { benchmarkLabel, evidenceVersionNote, getTradeThesisTimeline, isEvidenceVersionChange, listTradeTheses, metricLabelText, observationValue, rankText, stateLabel, textValue, thesisChartAnnotations, thesisItems } from './trade-thesis';
import type { TradeThesisSummary, TradeThesisTimeline } from './trade-thesis';

const props = defineProps<{ symbol: string; asOf?: string | null; marketAsOf?: string | null }>();
const emit = defineEmits<{ (event: 'annotations', annotations: WorkbenchAnnotation[]): void }>();
const items = ref<TradeThesisSummary[]>([]);
const loading = ref(false);
const error = ref('');
const timeline = ref<Record<string, TradeThesisTimeline>>({});
const timelineError = ref<Record<string, string>>({});
let controller: AbortController | null = null;
let timelineController: AbortController | null = null;
const polling = usePolling();

const hasHistoricalCutoff = computed(() => Boolean(props.asOf));

async function load() {
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = '';
  try {
    const payload = await listTradeTheses(props.symbol, props.asOf ?? undefined, controller.signal);
    items.value = thesisItems(payload);
    emit('annotations', thesisChartAnnotations(items.value));
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') error.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    loading.value = false;
  }
}

async function loadTimeline(thesisId: string) {
  if (timeline.value[thesisId]) return;
  timelineController?.abort();
  timelineController = new AbortController();
  timelineError.value = { ...timelineError.value, [thesisId]: '' };
  try {
    timeline.value = { ...timeline.value, [thesisId]: await getTradeThesisTimeline(thesisId, props.asOf ?? undefined, timelineController.signal) };
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') timelineError.value = { ...timelineError.value, [thesisId]: reason instanceof Error ? reason.message : String(reason) };
  }
}

function timelineRows(thesisId: string) {
  const value = timeline.value[thesisId];
  const rows = value?.items ?? value?.events ?? [...(value?.revisions ?? []), ...(value?.evaluations ?? [])];
  if (!props.asOf) return rows;
  const cutoff = props.asOf.length === 10 ? Date.parse(`${props.asOf}T23:59:59+08:00`) : Date.parse(props.asOf);
  return rows.filter((row) => {
    const point = row.cutoff_at ?? row.created_at ?? row.evaluated_at;
    return typeof point === 'string' && Number.isFinite(Date.parse(point)) && Date.parse(point) <= cutoff;
  });
}

function rankingsText(item: TradeThesisSummary, kind: 'original' | 'current') {
  const rankings = kind === 'original' ? item.original_rankings : item.current_rankings;
  const fallback = kind === 'original' ? item.original_rank : item.current_rank;
  return (rankings?.length ? rankings : fallback ? [fallback] : []).map(rankText).join('；') || '未提供';
}

function nonComparableReason(item: TradeThesisSummary) {
  const rank = item.current_rankings?.find((value) => value.comparable_to_previous === false) ?? item.current_rank;
  if (rank?.comparable_to_previous === false) return rank.non_comparable_reason || '策略或比较范围已改变';
  const original = item.original_rankings?.[0] ?? item.original_rank;
  const current = item.current_rankings?.[0] ?? item.current_rank;
  if (original && current && ((original.strategy ?? original.lane) !== (current.strategy ?? current.lane)
    || (original.universe_size ?? original.population) !== (current.universe_size ?? current.population))) {
    return '原始与当前属于不同策略或比较范围';
  }
  return '';
}

watch(() => [props.symbol, props.asOf] as const, () => {
  timelineController?.abort(); timeline.value = {}; timelineError.value = {}; void load();
});
onMounted(() => polling.every(60_000, load));
onBeforeUnmount(() => { controller?.abort(); timelineController?.abort(); polling.stop(); emit('annotations', []); });
</script>

<template>
  <section class="thesis-panel" data-testid="trade-thesis-panel">
    <header>
      <div><p class="eyebrow">交易假设生命周期 · 研究建议</p><h2>结论与变化</h2></div>
      <el-tag type="warning" effect="plain">不写推荐排名 / 不生成订单</el-tag>
    </header>
    <el-alert v-if="hasHistoricalCutoff" type="info" :closable="false" show-icon
      :title="`历史视图截至 ${asOf}；后续评价与事件不会被请求或展示`" />
    <p v-else-if="marketAsOf" class="market-date">行情数据截至 {{ marketAsOf }}；假设区默认读取最新已知评价。</p>
    <el-alert v-if="error" class="panel-error" type="error" :closable="false" show-icon :title="`交易假设加载失败：${error}`" />
    <el-skeleton v-if="loading && !items.length" :rows="4" animated />
    <el-empty v-else-if="!error && !items.length" description="该股票在所选截止时点没有可展示的交易假设" :image-size="72" />

    <article v-for="item in items" :key="`${item.thesis_id}-${item.revision}`" class="thesis-card">
      <div class="conclusion-row">
        <div><strong>{{ item.original_thesis || item.claim || '原假设未提供摘要' }}</strong><span class="phase">→ {{ stateLabel(item.current_phase || item.thesis_state) }}</span></div>
        <div class="state-tags"><el-tag>{{ stateLabel(item.thesis_state) }}</el-tag><el-tag type="info">证据 {{ stateLabel(item.evidence_status) }}</el-tag></div>
      </div>
      <dl class="decision-grid">
        <div><dt>本轮证据变化</dt><dd v-if="item.changes_since_previous?.length"><span v-for="(change, index) in item.changes_since_previous" :key="index">
          <template v-if="isEvidenceVersionChange(change)">{{ change.label || metricLabelText(change.metric) }}：当前 {{ observationValue(change.new_value, change.metric, change.unit) }}<small class="version-note">{{ evidenceVersionNote(change) }}；旧口径值见审计时间轴</small></template>
          <template v-else>{{ change.label || metricLabelText(change.metric) }}：{{ observationValue(change.old_value, change.metric, change.unit) }} → {{ observationValue(change.new_value, change.metric, change.unit) }}</template>
          <small v-if="change.benchmark">基准 {{ benchmarkLabel(change.benchmark) }}</small>
        </span></dd><dd v-else>首次评价，无上轮差异</dd></div>
        <div><dt>当前新买</dt><dd>研究条件：{{ item.new_buy || stateLabel(item.entry_state) }}<small v-if="item.scenario_projection">场景与纪律交集：{{ stateLabel(item.scenario_projection.combined_entry_state) }}</small></dd></div>
        <div><dt>已有持仓</dt><dd>{{ item.holding_action || item.holding_plan_status || '未加载有效持仓计划，不能由研究结论推导动作' }}</dd></div>
        <div><dt>下一验证点</dt><dd><span v-for="(check, index) in item.next_checks ?? []" :key="index">{{ textValue(check) }}</span><span v-if="!item.next_checks?.length">未提供</span></dd></div>
      </dl>
      <el-alert v-for="(conflict, index) in item.scenario_projection?.conflicts ?? []" :key="`conflict-${index}`"
        type="warning" :closable="false" :title="typeof conflict === 'string' ? conflict : conflict.message || conflict.code || '场景约束待核对'" />
      <p v-if="item.scenario_projection?.execution" class="market-date">可执行性：{{ stateLabel(item.scenario_projection.execution.state) }}；{{ item.scenario_projection.execution.reason }}<span v-if="item.scenario_projection.execution.earliest_session">；最早交易日 {{ item.scenario_projection.execution.earliest_session }}</span></p>
      <div v-if="item.observations?.length" class="facts">
        <h4>当前观察事实</h4>
        <span v-for="(fact, index) in item.observations" :key="fact.evidence_id || index">
          <b>{{ metricLabelText(fact.metric) }}</b>{{ observationValue(fact.value, fact.metric, fact.unit) }}<small>基准 {{ benchmarkLabel(fact.benchmark) }}</small>
        </span>
      </div>
      <div class="rank-grid">
        <span><b>原始范围</b>{{ rankingsText(item, 'original') }}</span>
        <span><b>当前范围</b>{{ rankingsText(item, 'current') }}</span>
        <el-tag v-if="nonComparableReason(item)" type="warning" effect="dark">不可直接比较：{{ nonComparableReason(item) }}</el-tag>
      </div>
      <details>
        <summary @click.once="loadTimeline(item.thesis_id)">审计与时间轴</summary>
        <dl class="audit-grid">
          <div><dt>cutoff</dt><dd>{{ item.cutoff_at || asOf || '未提供' }}</dd></div><div><dt>revision</dt><dd>{{ item.revision }}</dd></div>
          <div><dt>source run</dt><dd>{{ item.source_run_id || '未提供' }}</dd></div><div><dt>evaluation</dt><dd>{{ item.evaluation_id || '未提供' }}</dd></div>
          <div><dt>input hash</dt><dd>{{ item.input_hash || '未提供' }}</dd></div><div><dt>content hash</dt><dd>{{ item.content_hash || '未提供' }}</dd></div>
          <div><dt>来源模式</dt><dd>{{ item.origin_mode || '未提供' }}</dd></div><div><dt>available_at</dt><dd>{{ item.available_at || '未提供' }}</dd></div>
        </dl>
        <el-alert v-if="timelineError[item.thesis_id]" type="error" :closable="false" :title="`时间轴加载失败：${timelineError[item.thesis_id]}`" />
        <ol v-else-if="timelineRows(item.thesis_id).length" class="timeline"><li v-for="(row, index) in timelineRows(item.thesis_id)" :key="index"><code>{{ JSON.stringify(row) }}</code></li></ol>
        <p v-else class="muted">展开后加载修订、证据变化与阶段转换。</p>
      </details>
    </article>
  </section>
</template>

<style scoped>
.thesis-panel { margin: 14px 0; padding: 16px; border: 1px solid #dbeafe; border-radius: 12px; background: #f8fbff; }
header,.conclusion-row { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; } h2 { margin:0; font-size:20px; }.eyebrow { margin:0 0 4px; color:#2563eb; font-size:11px; letter-spacing:.08em; }.panel-error { margin:12px 0; }
.thesis-card { margin-top:12px; padding:14px; border:1px solid #dbe3ee; border-radius:10px; background:white; }.phase { margin-left:8px; color:#475569; }.state-tags { display:flex; gap:6px; }.market-date { margin:10px 0 0; color:#64748b; font-size:12px; }.decision-grid { display:grid; grid-template-columns:2fr 1fr 1.3fr 1.5fr; gap:10px; margin:14px 0; }.decision-grid>div,.audit-grid>div { padding:9px; border-radius:7px; background:#f8fafc; }dt { color:#64748b; font-size:11px; }dd { margin:4px 0 0; line-height:1.55; }dd span { display:block; }dd small { display:block; color:#64748b; }.version-note { color:#b45309; font-weight:600; }.facts { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 12px; }.facts h4 { width:100%; margin:0; color:#475569; font-size:12px; }.facts span { padding:7px 9px; border:1px solid #dbeafe; border-radius:7px; background:#eff6ff; }.facts b { margin-right:6px; }.facts small { display:block; color:#64748b; }.rank-grid { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }.rank-grid>span { color:#475569; }.rank-grid b { margin-right:6px; color:#0f172a; }details { margin-top:12px; }summary { cursor:pointer; color:#2563eb; }.audit-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; margin-top:10px; }.audit-grid dd { overflow-wrap:anywhere; font-family:ui-monospace,monospace; font-size:11px; }.timeline code { white-space:pre-wrap; overflow-wrap:anywhere; }.muted { color:#64748b; }
@media(max-width:900px){.decision-grid,.audit-grid{grid-template-columns:1fr}.conclusion-row,header{flex-direction:column}.state-tags{flex-wrap:wrap}}
</style>

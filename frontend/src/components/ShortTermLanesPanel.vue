<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue';
import { getJson } from '../api/http';
import ReviewSelectionPanel from './ReviewSelectionPanel.vue';
import RecommendationPoolPanel from './RecommendationPoolPanel.vue';
import ShortTermLaneDetails from './ShortTermLaneDetails.vue';
import ObservationFollowup from './ObservationFollowup.vue';
import StrategyEffectiveness from './StrategyEffectiveness.vue';
import StrategyResultSummary from './StrategyResultSummary.vue';
import EventResearchOverlay from './EventResearchOverlay.vue';
import { downloadStrategyReport, type StrategyScan, type StrategyLane, type StrategyReport } from './short-term-reports';

const props = defineProps<{ summary?: Record<string, unknown>; recommendation?: unknown }>();
const loadedDetails = ref<Partial<StrategyScan>>({});
const selected = ref('overview');
const scan = computed(() => {
  const base = props.summary?.strategy_lanes as StrategyScan | undefined;
  return base ? { ...base, ...loadedDetails.value } : undefined;
});
const detailError = ref(''); const loadingDetail = ref(false);
let detailController: AbortController | undefined;
watch(() => (props.summary?.strategy_lanes as StrategyScan | undefined)?.detail_run_id, () => {
  detailController?.abort(); loadedDetails.value = {}; detailError.value = ''; loadingDetail.value = false;
});
onBeforeUnmount(() => detailController?.abort());
watch(selected, () => {
  detailController?.abort(); loadingDetail.value = false; detailError.value = '';
  const retained = { ...loadedDetails.value };
  delete retained.followup; delete retained.effectiveness;
  loadedDetails.value = retained;
});
async function loadDetail(section: 'followup' | 'effectiveness' | 'research' | 'events', append = false) {
  const run = scan.value?.detail_run_id;
  const scope = selected.value;
  if (!run || loadingDetail.value) return;
  detailController = new AbortController(); const controller = detailController;
  loadingDetail.value = true; detailError.value = '';
  const old = loadedDetails.value.followup?.items ?? [];
  const oldGroups = loadedDetails.value.effectiveness?.groups ?? [];
  const offset = section === 'effectiveness' ? oldGroups.length : old.length;
  const query = new URLSearchParams({ run_id: run, section, limit: '40', offset: String(append ? offset : 0) });
  if (['followup', 'effectiveness'].includes(section) && scope !== 'overview') query.set('key', scope);
  try {
    const response = await getJson<{detail: unknown}>(`/api/research/strategy/post-close/detail?${query}`, {signal: controller.signal});
    if (scan.value?.detail_run_id !== run || selected.value !== scope || controller.signal.aborted) return;
    if (section === 'research') loadedDetails.value = {...loadedDetails.value, ...response.detail as Partial<StrategyScan>};
    else if (section === 'followup') {
      const next = response.detail as NonNullable<StrategyScan['followup']>;
      loadedDetails.value.followup = {...next, items: append ? [...old, ...next.items] : next.items};
    } else if (section === 'effectiveness') {
      const next = response.detail as NonNullable<StrategyScan['effectiveness']>;
      loadedDetails.value.effectiveness = {...next, groups: append ? [...oldGroups, ...next.groups] : next.groups};
    }
    else loadedDetails.value.event_research = response.detail as StrategyScan['event_research'];
  } catch (error) { if (!controller.signal.aborted) detailError.value = String(error); }
  finally { if (detailController === controller) loadingDetail.value = false; }
}
async function downloadReport(report: StrategyReport) {
  if (report.markdown) return downloadStrategyReport(report);
  const run = scan.value?.detail_run_id; if (!run) return;
  try {
    const query = new URLSearchParams({run_id: run, section: 'report', key: report.key});
    const response = await getJson<{detail: StrategyReport}>(`/api/research/strategy/post-close/detail?${query}`);
    downloadStrategyReport(response.detail);
  } catch (error) { detailError.value = String(error); }
}
const moreFollowup = computed(() => !loadedDetails.value.followup || loadedDetails.value.followup.page_total == null || loadedDetails.value.followup.items.length < loadedDetails.value.followup.page_total);
const moreEffectiveness = computed(() => !loadedDetails.value.effectiveness || loadedDetails.value.effectiveness.page_total == null || loadedDetails.value.effectiveness.groups.length < loadedDetails.value.effectiveness.page_total);
const reports = computed(() => scan.value?.report_bundle?.reports ?? []);
const currentReport = computed(() => reports.value.find(r => r.key === selected.value));
const currentLane = computed(() => scan.value?.lanes.find(l => l.key === selected.value));
watch(reports, list => {
  if (!list.some(r => r.key === selected.value)) selected.value = 'overview';
});
const reportFor = (key: string) => reports.value.find(r => r.key === key);
const leadName = (lane: StrategyLane) => {
  const pick = lane.selected[0] ?? lane.caution_list?.[0];
  return pick ? `${lane.selected[0] ? '' : '风险观察：'}${pick.name}（${pick.symbol.split('.')[0]}）` : '无候选';
};
</script>

<template>
  <el-card shadow="never" class="lane-panel section-gap">
    <template #header>
      <div class="lane-heading"><div><strong>多策略短线报告</strong><p>每个策略独立展开，总报告汇总比较；观察条件不等于买入指令。</p></div>
        <el-tag :type="scan?.status === 'completed' ? 'success' : 'warning'">{{ scan?.as_of_date ?? '尚无扫描' }}</el-tag></div>
    </template>
    <RecommendationPoolPanel :value="recommendation ?? summary?.recommendation_pool" />
    <el-alert v-if="!scan || scan.status !== 'completed'" title="多策略历史数据未齐：未生成有效名单，不使用旧名单冒充今日结果。" type="warning" :closable="false"/>
    <template v-if="scan">
      <div v-if="scan.details_deferred" class="detail-controls" aria-label="按需加载同轮证据">
        <button :disabled="loadingDetail" @click="loadDetail('research')">公司研究详情</button>
        <button :disabled="loadingDetail" @click="loadDetail('events')">消息影响详情</button>
        <button :disabled="loadingDetail || !moreEffectiveness" @click="loadDetail('effectiveness', !!loadedDetails.effectiveness)">策略效果样本{{ !moreEffectiveness ? ' · 已加载全部' : loadedDetails.effectiveness ? ' · 再加载40组' : '' }}</button>
        <button :disabled="loadingDetail || !moreFollowup" @click="loadDetail('followup', !!loadedDetails.followup)">往期跟踪 · {{ !moreFollowup ? '已加载全部' : loadedDetails.followup ? '再加载40条' : '加载40条' }}</button>
        <span v-if="loadingDetail" role="status">正在读取同轮证据…</span>
      </div>
      <p v-if="detailError" role="alert">{{ detailError }}</p>
      <p class="lane-meta">完整历史 {{ scan.coverage.complete_history }} / {{ scan.coverage.universe }} 只主板股票。所有报告使用同一轮扫描数据。</p>
      <el-alert v-if="!reports.length" title="这轮旧结果尚无独立报告，请在重新生成后查看；不以拼接旧名单冒充报告。" type="warning" :closable="false"/>
      <template v-else>
        <nav class="report-nav" aria-label="短线策略报告">
          <button v-for="report in reports" :key="report.key" :data-report-key="report.key" :aria-pressed="selected === report.key"
            @click="selected = report.key">{{ report.key === 'overview' ? '总报告' : scan.lanes.find(l => l.key === report.key)?.label ?? report.title }}</button>
        </nav>
        <section v-if="currentReport" class="report-body" :data-report-view="selected">
          <div class="report-heading"><h2>{{ currentReport.title }}</h2><button class="download" @click="downloadReport(currentReport)">下载本报告 · Markdown</button></div>
          <p class="lane-meta">{{ currentReport.as_of_date }} · {{ scan.version }}</p>
          <template v-if="selected === 'overview'">
            <h3>本次结论</h3>
            <template v-for="report in reports" :key="report.key">
              <StrategyResultSummary v-if="report.result_summary" :result="report.result_summary" compact />
            </template>
            <EventResearchOverlay :historical="scan.event_research" />
            <h3>策略对照与独立报告</h3>
            <div class="table-scroll"><table><thead><tr><th>策略</th><th>匹配 / 展示</th><th>首位代表</th><th>公司证据覆盖</th></tr></thead>
              <tbody><tr v-for="lane in scan.lanes" :key="lane.key">
                <td><button class="text-link" :aria-label="`打开${lane.label}独立报告`" @click="selected = lane.key">{{ lane.label }}</button></td>
                <td>{{ lane.total_matches }} / {{ lane.selected.length }}</td>
                <td>{{ leadName(lane) }}</td>
                <td>{{ reportFor(lane.key)?.review?.review_coverage.selected_reviewed ?? 0 }} / {{ lane.selected.length }} 展示候选</td>
              </tr></tbody></table></div>
            <h3>跨策略重复与分歧</h3>
            <p class="lane-meta">重复出现不是独立利好计票，也不会自动提高仓位。</p>
            <article v-for="stock in scan.report_bundle?.overlaps" :key="stock.symbol" class="overlap">
              <strong>{{ stock.name }}（{{ stock.symbol.split('.')[0] }}）</strong>
              <p>{{ stock.memberships.map(m => `${m.label}（${m.state}）`).join('；') }}</p><p class="lane-meta">{{ stock.interpretation }}</p>
            </article>
            <p v-if="!scan.report_bundle?.overlaps.length" class="lane-meta">当前展示范围没有跨策略重复股票。</p>
            <h3>各策略候选索引</h3>
            <p v-for="lane in scan.lanes" :key="lane.key" class="candidate-index"><strong>{{ lane.label }}：</strong>{{ lane.selected.map(p => `${p.name}（${p.symbol.split('.')[0]}）`).join('、') || lane.empty_reason }}</p>
            <ReviewSelectionPanel :scan="scan" />
            <StrategyEffectiveness v-if="!scan.details_deferred || scan.effectiveness" :value="scan.effectiveness" :lanes="scan.lanes" />
            <ObservationFollowup v-if="!scan.details_deferred || scan.followup" :followup="scan.followup" />
          </template>
          <template v-else-if="currentLane">
            <StrategyResultSummary v-if="currentReport.result_summary" :result="currentReport.result_summary" />
            <el-alert v-else title="本轮尚无新版结论摘要，请查看下方原始研究；不把旧格式标为已更新。" type="warning" :closable="false" />
            <EventResearchOverlay :historical="scan.event_research" :symbols="currentLane.selected.map(s=>s.symbol)" />
            <ReviewSelectionPanel v-if="currentReport.review" :scan="currentReport.review" />
            <ShortTermLaneDetails :lane="currentLane" />
            <h3>数据与筛选说明</h3>
            <p class="lane-purpose">本策略要找什么：{{ currentLane.purpose }}</p>
            <p class="lane-meta">公司证据覆盖 {{ currentReport.review?.review_coverage.selected_reviewed ?? 0 }} / {{ currentLane.selected.length }} 展示候选；首位代表完成不表示全部完成。</p>
            <p v-if="currentLane.key === 'event'" class="lane-meta">已核验事件覆盖 {{ scan.coverage.verified_event_symbols ?? 0 }} 只；没有匹配不等于全市场没有事件。</p>
            <StrategyEffectiveness v-if="!scan.details_deferred || scan.effectiveness" :value="scan.effectiveness" :lane="currentLane.key" :lanes="scan.lanes" />
            <ObservationFollowup v-if="!scan.details_deferred || scan.followup" :followup="scan.followup" :lane="currentLane.key" />
          </template>
          <p v-if="scan.market" class="lane-meta">共同市场背景：主板有效样本上涨占比 {{ (scan.market.up_fraction * 100).toFixed(1) }}%，十日收益中位数 {{ scan.market.median_return10.toFixed(2) }}%。这不是完整大盘研判。</p>
          <p class="report-boundary">{{ scan.notice }} 各策略分数不可跨策略相加。暂无独立样本外盈利验证，不自动下单或修改自选。</p>
        </section>
      </template>
    </template>
  </el-card>
</template>

<style scoped>
.detail-controls{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}.detail-controls button{border:1px solid var(--el-border-color);background:var(--el-bg-color);color:var(--el-color-primary);border-radius:7px;padding:9px 12px}.detail-controls button:disabled{opacity:.5;cursor:wait}
.lane-heading,.report-heading{display:flex;align-items:center;justify-content:space-between;gap:1rem}.lane-heading p,.lane-meta{color:var(--el-text-color-secondary);font-size:12px;line-height:1.8}.lane-heading p{margin:6px 0 0}.report-nav{display:flex;flex-wrap:wrap;gap:8px;padding:12px 0 18px;border-bottom:1px solid var(--el-border-color-lighter)}button{font:inherit;cursor:pointer}.report-nav button,.download{border:1px solid var(--el-border-color);border-radius:7px;padding:9px 14px;background:var(--el-bg-color);color:var(--el-text-color-regular);font-size:13px}.report-nav button[aria-pressed="true"]{background:var(--el-color-primary-light-9);border-color:var(--el-color-primary);color:var(--el-color-primary)}button:focus-visible{outline:2px solid var(--el-color-primary);outline-offset:3px}.report-heading{margin-top:20px}.report-heading h2{font-size:20px;margin:0}.report-body h3{font-size:16px;margin:24px 0 12px}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--el-border-color-lighter);white-space:nowrap}th{background:var(--el-fill-color-light);font-weight:500;color:var(--el-text-color-secondary)}.text-link{background:none;border:0;padding:0;color:var(--el-color-primary)}.overlap{border-left:3px solid var(--el-border-color);padding:4px 16px;margin:14px 0;font-size:13px}.overlap p{margin:6px 0;line-height:1.8}.candidate-index,.lane-purpose{font-size:13px;line-height:1.9}.report-boundary{font-size:12px;color:var(--el-text-color-secondary);line-height:1.8;border-top:1px solid var(--el-border-color-lighter);padding-top:16px;margin-top:24px}@media(max-width:680px){.report-heading{align-items:flex-start;flex-direction:column}.report-nav button{padding:8px 11px}}
</style>

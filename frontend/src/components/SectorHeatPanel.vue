<script setup lang="ts">
import { computed, defineAsyncComponent, h, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { getJson } from '../api/http';
import type { SectorHeatHistoryPoint, SectorHeatItem, SectorHeatKind, SectorHeatSnapshot } from '../types/sector-heat';
import type { EChartsOption } from 'echarts';
import { observedHistory, signedPercent } from './sector-heat-presentation';

const loadChartModule = () => import('./SectorHeatChart.vue');
const VChart = defineAsyncComponent({
  loader: loadChartModule,
  loadingComponent: { render: () => h('div', { role: 'status' }, '正在加载图表，排行榜和文字详情已可查看…') },
  errorComponent: { render: () => h('div', { role: 'alert' }, '图表资源加载失败；排行榜和文字详情仍可使用，请刷新重试。') },
  delay: 0, timeout: 15000,
});

type SortKey = 'attention' | 'activity' | 'strength' | 'funding';
const snapshot = ref<SectorHeatSnapshot | null>(null);
const loading = ref(false);
const error = ref('');
const selectedKind = ref<SectorHeatKind>('industry');
const query = ref('');
const sortKey = ref<SortKey>('attention');
const selectedKey = ref('');
const historyMode = ref<'daily' | 'intraday'>('daily');
const fullHistory = ref(false);
let controller: AbortController | null = null;
let detailController: AbortController | null = null;
const detailCache = reactive(new Map<string, SectorHeatItem>());
const detailError = ref('');
const detailLoading = ref(false);

const items = computed(() => {
  const rows = (snapshot.value?.items ?? []).filter((item) => item.kind === selectedKind.value);
  const needle = query.value.trim().toLowerCase();
  return rows.filter((item) => !needle || `${item.name} ${item.code}`.toLowerCase().includes(needle)).sort((a, b) => {
    const value = (item: SectorHeatItem): number | null => { const raw = sortKey.value === 'activity' ? item.activity_score : sortKey.value === 'strength' ? item.strength_score : sortKey.value === 'funding' ? item.metrics?.main_net : item.attention_score; return typeof raw === 'number' && Number.isFinite(raw) ? raw : null; };
    const av = value(a); const bv = value(b);
    if (av === null && bv !== null) return 1;
    if (bv === null && av !== null) return -1;
    if (av === null && bv === null) return 0;
    return (bv ?? 0) - (av ?? 0);
  });
});
const selectedListItem = computed(() => items.value.find((item) => item.key === selectedKey.value) ?? items.value[0] ?? null);
const selected = computed(() => (selectedListItem.value ? detailCache.get(selectedListItem.value.key) ?? selectedListItem.value : null));
const history = computed(() => (selected.value ? (historyMode.value === 'intraday' ? selected.value.intraday_history : selected.value.daily_history) ?? [] : []));
const chartHistory = computed(() => observedHistory(history.value, fullHistory.value));
const categoryItems = computed(() => (snapshot.value?.items ?? []).filter((item) => item.kind === selectedKind.value));
const leaders = computed(() => [...categoryItems.value].filter(item => item.attention_score != null).sort((a,b) => b.attention_score! - a.attention_score!).slice(0,3));
const coverageCount = computed(() => categoryItems.value.filter(item => item.coverage === 1).length);
const missingLabels: Record<string, string> = { strength: '相对强度', breadth: '上涨广度', activity: '成交活跃度', flow: '资金方向', advancing_breadth: '上涨广度', flow_ratio: '资金/成交额比', volume_ratio: '量比', speed: '涨速', return_pct: '涨跌幅', amount: '成交额', main_net: '大单口径净额', member_count: '广度样本数', persistence: '持续性' };
const stateLabels: Record<string, string> = { warming: '升温', cooling: '降温', active: '活跃', weak: '偏弱', divergent: '分歧', stable: '稳定' };
const qualityLabels: Record<string, string> = { ok: '可用', ready: '可用', degraded: '部分缺失或降级', partial: '部分可用', missing: '缺失', stale: '过期' };

function score(value?: number | null) { return value === null || value === undefined || Number.isNaN(value) ? '—' : Math.max(0, Math.min(100, value)).toFixed(0); }
function pct(value?: number | null) { return value === null || value === undefined ? '—' : `${(value * 100).toFixed(0)}%`; }
function metric(item: SectorHeatItem, key: string) { const value = item.metrics?.[key]; return value === null || value === undefined || value === '' ? '—' : key === 'amount' || key === 'main_net' ? amount(value) : String(value); }
function amount(value: number | string) { if (typeof value === 'string' && /万|亿|元/.test(value)) return value; const n = Number(value); if (!Number.isFinite(n)) return '—'; const a = Math.abs(n); return a >= 1e8 ? `${(n / 1e8).toFixed(2)} 亿元` : a >= 1e4 ? `${(n / 1e4).toFixed(2)} 万元` : `${n.toFixed(0)} 元`; }
function missingText(value: string) { return missingLabels[value] ?? value; }
function reducedMotion() { return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches; }
function dateTime(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—'; }
function phaseLabel(value?: string | null) { const labels: Record<string, string> = { intraday: '盘中', open30: '早盘', midday: '午间', afternoon30: '午后', tail: '尾盘', close: '收盘' }; return labels[value ?? ''] ?? value ?? '—'; }
function select(item: SectorHeatItem) { selectedKey.value = item.key; }

const chartOption = computed<EChartsOption>(() => {
  const points = chartHistory.value;
  const labels = points.map((point) => historyMode.value === 'intraday' && point.data_as_of ? new Date(point.data_as_of).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }) : `${point.trade_date ?? ''}${point.phase ? ` ${phaseLabel(point.phase)}` : ''}`);
  const series = (name: string, key: 'attention_score' | 'activity_score' | 'strength_score' | 'risk_score', color: string) => ({ name, type: 'line' as const, data: points.map((point) => point[key] ?? null), connectNulls: false, showSymbol: true, symbolSize: 5, smooth: false, itemStyle: { color }, lineStyle: { color, width: key === 'attention_score' ? 3 : 2 } });
  return { animation: !reducedMotion(), animationDuration: 240, tooltip: { trigger: 'axis', confine: true }, legend: { top: 8, itemWidth: 16, textStyle: { color: '#53627b', fontSize: 12 }, selected: { '分歧': false } }, grid: { left: 42, right: 16, top: 52, bottom: 40 }, xAxis: { type: 'category', data: labels, axisLabel: { hideOverlap: true, color: '#6b7890', formatter: (value: string) => value.replace(/^\d{4}-/, '').replace(/ 收盘$/, '') }, axisLine: { lineStyle: { color: '#dce3ed' } }, axisTick: { show: false } }, yAxis: { type: 'value', min: 0, max: 100, splitLine: { lineStyle: { color: '#edf1f6' } }, axisLabel: { color: '#6b7890' } }, series: [series('关注', 'attention_score', '#4f5dde'), series('活跃', 'activity_score', '#d39239'), series('方向', 'strength_score', '#188b80'), series('分歧', 'risk_score', '#c65968')] };
});

async function readHeat(path: string, requestController: AbortController) {
  const timer = window.setTimeout(() => requestController.abort('timeout'), 15000);
  try {
    return await getJson<{ sector_heat?: SectorHeatSnapshot }>(path, { signal: requestController.signal });
  } catch (cause) {
    if (requestController.signal.reason === 'timeout') throw new Error('请求超过15秒，请点击刷新重试；不能据此判断没有数据');
    throw cause;
  } finally { window.clearTimeout(timer); }
}

async function load() {
  const requestController = new AbortController();
  controller?.abort();
  controller = requestController;
  detailController?.abort(); detailController = null; detailLoading.value = false;
  loading.value = true; error.value = ''; detailCache.clear(); detailError.value = '';
  try {
    const response = await readHeat('/api/v1/sector-heat', requestController);
    if (controller !== requestController) return;
    snapshot.value = response.sector_heat ?? null;
    if (!items.value.some((item) => item.key === selectedKey.value)) selectedKey.value = items.value[0]?.key ?? '';
  } catch (cause) {
    if (controller === requestController && (cause as Error).name !== 'AbortError') error.value = cause instanceof Error ? cause.message : String(cause);
  } finally { if (controller === requestController) loading.value = false; }
  if (controller === requestController && !error.value && selectedListItem.value) await loadDetail(selectedListItem.value.key);
}
async function loadDetail(key: string) {
  detailController?.abort(); detailController = null;
  detailError.value = ''; detailLoading.value = false;
  if (detailCache.has(key)) return;
  const requestController = new AbortController(); detailController = requestController;
  detailLoading.value = true; detailError.value = '';
  try {
    const response = await readHeat(`/api/v1/sector-heat?key=${encodeURIComponent(key)}`, requestController);
    if (detailController !== requestController) return;
    const item = response.sector_heat?.items?.find((candidate) => candidate.key === key);
    if (!item) throw new Error('详情未返回所选板块');
    detailCache.set(key, item);
  } catch (cause) {
    if (detailController === requestController && (cause as Error).name !== 'AbortError') detailError.value = cause instanceof Error ? cause.message : String(cause);
  } finally { if (detailController === requestController) detailLoading.value = false; }
}
watch(() => selectedListItem.value?.key, (key) => { if (key && !loading.value) void loadDetail(key); }, { flush: 'sync' });
onMounted(() => {
  void load();
  // Warm the optional visual in parallel, never await it before rendering facts.
  void loadChartModule().catch(() => { /* The async component owns the visible error. */ });
});
onBeforeUnmount(() => { controller?.abort(); controller = null; detailController?.abort(); detailController = null; });
</script>

<template>
  <section class="sector-heat-panel" :data-detail-key="selectedListItem && detailCache.has(selectedListItem.key) ? selectedListItem.key : ''">
    <div class="session-bar">
      <div><span class="live-dot"></span><strong>{{ snapshot?.trade_date || '读取中' }}</strong><span>{{ phaseLabel(snapshot?.phase) }}快照</span></div>
      <button class="quiet-button" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </div>
    <div v-if="error" role="alert" class="notice danger">板块热度读取失败：{{ error }}</div>
    <div v-else-if="loading && !snapshot" role="status" class="empty-state">正在读取板块排行榜…</div>
    <div v-else-if="!snapshot" class="empty-state">板块热度尚未发布，等待快照生成。</div>
    <template v-else>
      <section class="overview-strip" aria-label="当前类别概览">
        <div class="overview-intro"><span class="eyebrow">本期观察</span><strong>热度集中在哪里</strong><span>{{ selectedKind === 'industry' ? '行业' : '概念' }} · {{ categoryItems.length }} 个样本，非全市场覆盖</span></div>
        <button v-for="(item, index) in leaders" :key="item.key" class="leader-card" @click="query = ''; select(item)">
          <span class="eyebrow">关注排序 0{{ index + 1 }}</span><strong>{{ item.name }}</strong><span>关注 <b>{{ score(item.attention_score) }}</b> <span class="leader-state">{{ stateLabels[item.state_label || ''] || item.state_label || '—' }}</span></span>
        </button>
      </section>
      <div class="workspace-grid">
        <aside class="ranking-pane" aria-label="板块排行榜">
          <div class="ranking-tools">
            <div class="segmented" aria-label="分类">
              <button :class="{ active: selectedKind === 'industry' }" :aria-pressed="selectedKind === 'industry'" @click="selectedKind = 'industry'; query = ''">行业</button>
              <button :class="{ active: selectedKind === 'concept' }" :aria-pressed="selectedKind === 'concept'" @click="selectedKind = 'concept'; query = ''">概念</button>
            </div>
            <input v-model="query" type="search" placeholder="搜索名称或代码" aria-label="搜索名称或代码" />
            <div class="rank-sort"><span>{{ items.length }} / {{ categoryItems.length }} 个</span><select v-model="sortKey" aria-label="排行榜排序"><option value="attention">按关注分</option><option value="activity">按活跃度</option><option value="strength">按方向强度</option><option value="funding">按资金净额</option></select></div>
          </div>
          <div class="ranking-columns"><span>板块 / 状态</span><span>关注</span><span>方向</span></div>
          <div class="ranking-list" tabindex="0" aria-label="可滚动的板块列表">
            <button v-for="item in items" :key="item.key" class="ranking-row" :class="{ selected: selected?.key === item.key }" :aria-label="item.name" :aria-pressed="selected?.key === item.key" @click="select(item)">
              <span class="rank-number">{{ item.rank ?? '—' }}</span><span class="rank-name"><strong>{{ item.name }}</strong><small>{{ stateLabels[item.state_label || ''] || item.state_label || '—' }}<span v-if="item.coverage != null && item.coverage < 1"> · 数据不全</span></small></span>
              <span class="rank-score">{{ score(item.attention_score) }}<i :style="{ width: (item.attention_score ?? 0) + '%' }"></i></span><span class="rank-strength">{{ score(item.strength_score) }}</span>
            </button>
            <div v-if="!items.length" class="empty-state">没有匹配的板块<br /><button class="quiet-button" @click="query = ''">清除搜索</button></div>
          </div>
          <div class="ranking-footer">关注分用于比较，不是上涨概率</div>
        </aside>
        <article class="detail-pane" aria-label="所选板块详情">
          <template v-if="selected">
            <header class="detail-header">
              <div><div class="eyebrow">{{ selectedKind === 'industry' ? '行业观察' : '概念观察' }} · {{ selected.code }}</div><h2>{{ selected.name }} <span class="state-badge">{{ stateLabels[selected.state_label || ''] || selected.state_label || '—' }}</span></h2></div>
              <div class="attention-score"><strong>{{ score(selected.attention_score) }}</strong><span>关注分 / 100</span></div>
            </header>
            <div v-if="detailLoading" role="status" class="notice">正在读取所选板块的历史、原因与因子…</div>
            <div v-if="detailError" role="alert" class="notice danger">板块详情读取失败：{{ detailError }}（列表仍可用）<button class="quiet-button" @click="loadDetail(selected.key)">重试</button></div>
            <div class="metric-grid">
              <div><span>板块涨跌</span><strong :class="Number(selected.metrics?.return_pct) > 0 ? 'up' : Number(selected.metrics?.return_pct) < 0 ? 'down' : ''">{{ signedPercent(selected.metrics?.return_pct) }}</strong></div>
              <div><span>成交额</span><strong>{{ metric(selected, 'amount') }}</strong></div>
              <div><span>大单口径净额</span><strong :class="Number(selected.metrics?.main_net) > 0 ? 'up' : Number(selected.metrics?.main_net) < 0 ? 'down' : ''">{{ metric(selected, 'main_net') }}</strong></div>
              <div><span>成交活跃度</span><strong>{{ score(selected.activity_score) }}<small> / 100</small></strong></div>
            </div>
            <div class="reading-note">
              <strong>怎样理解</strong><p v-if="selected.reasons?.length">{{ selected.reasons.slice(0, 3).join('；') }}</p><p v-else>{{ detailLoading ? '正在载入本次评分依据。' : '当前没有可核对的解释，仅展示已有指标，不推断买卖机会。' }}</p>
              <span v-if="selected.missing?.length" class="coverage-warning">缺少{{ selected.missing.map(missingText).join('、') }} · 有效维度 {{ pct(selected.coverage) }}</span>
            </div>
            <section class="trend-section" aria-label="热度历史">
              <div class="chart-heading"><div><h3>热度轨迹</h3><span>关注、活跃与方向的变化 · 0–100</span></div><div class="segmented"><button :class="{ active: historyMode === 'daily' }" @click="historyMode = 'daily'">日间</button><button :class="{ active: historyMode === 'intraday' }" @click="historyMode = 'intraday'">日内</button></div></div>
              <VChart v-if="chartHistory.length" class="heat-chart" :option="chartOption" autoresize />
              <div v-else class="empty-chart">{{ detailLoading ? '正在读取历史数据…' : '暂无该口径历史轨迹，不补画趋势' }}</div>
              <div class="chart-caption"><span>缺失处断线，单次观测显示为点；分歧线可点图例开启。</span><label><input v-model="fullHistory" type="checkbox" />完整历史窗口</label></div>
            </section>
            <details class="evidence-section">
              <summary>评分依据与数据质量 <span>覆盖 {{ pct(selected.coverage) }} · {{ qualityLabels[selected.quality || ''] || selected.quality || '—' }}</span></summary>
              <div class="evidence-content">
                <p class="evidence-intro">分数是对已观察市场的描述，不是上涨概率。分歧分 {{ score(selected.risk_score) }}，低分不代表没有其他风险。</p>
                <div v-for="factor in selected.factors ?? []" :key="factor.key" class="factor-row"><div><strong>{{ factor.label }}</strong><span>分数 {{ score(factor.score) }} · 贡献 {{ factor.contribution == null ? '—' : Number(factor.contribution).toFixed(1) }}</span></div><p>{{ factor.description }}</p></div>
                <p v-for="reason in selected.reasons?.slice(3) ?? []" :key="reason">{{ reason }}</p>
                <dl class="data-times"><dt>数据时点</dt><dd>{{ dateTime(selected.data_as_of) }}</dd><dt>接收时间</dt><dd>{{ dateTime(selected.received_at) }}</dd><dt>最近尝试</dt><dd>{{ dateTime(selected.selection?.latest_attempt_at) }}</dd><dt>广度样本数</dt><dd>{{ metric(selected, 'member_count') }}</dd></dl>
                <p v-if="selected.selection?.used_earlier_usable" class="coverage-warning">采用较早可用样本：{{ selected.selection.reason }}</p>
              </div>
            </details>
          </template>
          <div v-else class="empty-state">请选择一个板块查看</div>
        </article>
      </div>
      <details class="snapshot-details"><summary>本期数据说明 <span>完整维度 {{ coverageCount }} / {{ categoryItems.length }} 个板块<template v-if="snapshot.warnings?.length"> · 存在数据缺口</template></span></summary><div>
        <p v-for="warning in snapshot.warnings ?? []" :key="warning">{{ warning }}</p>
        <p>来源：{{ snapshot.source || '—' }} · 模型：{{ snapshot.model_version || '—' }} · 快照生成：{{ dateTime(snapshot.generated_at) }}</p>
        <p>历史以当前模型重算；排名不可比较时不显示升降。大单净额是供应商订单大小口径，不代表机构身份，也不是暗盘数据。</p>
      </div></details>
    </template>
  </section>
</template>

<style scoped src="./sector-heat-panel.css"></style>

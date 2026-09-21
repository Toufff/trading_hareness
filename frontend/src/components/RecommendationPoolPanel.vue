<script setup lang="ts">
import { computed } from 'vue';
import DisciplinePoolCard from './discipline/DisciplinePoolCard.vue';
type Pick = { symbol: string; name: string; priority: number; stage: string; sector?: string; business?: string; why_now: string; comparison: string; trigger: string; invalidation: string; company_risk: string; recommendation_note?: Note; ranking_reference?: RankingReference };
type SectorProxy = { kind: string; symbol?: string; name?: string; url?: string; published_date?: string };
type SectorView = { assessment: string; trend: string; volume_price: string; proxy: SectorProxy };
type Note = { rank_assessment: string; entry_reason: string; priority_reason: string; carry_over_reason?: string; weak_sector_entry_reason?: string; sector_disagreement_reason?: string; sector_view?: SectorView; sector_peers: { symbol: string; why_not: string }[]; information_checks: { topic: string; finding: string; url: string; published_date: string }[] };
type PeerFact = { symbol: string; name?: string; scope?: string; sector_label?: string; sector_position?: number; best_lane?: string; best_rank?: number; best_population?: number; metrics?: Record<string, number | null> };
type SectorOverview = { label?: string; members?: number; up_fraction?: number; return10_median?: number; recent_breadth?: number; breadth_acceleration?: number; flow_3d?: number; relative_strength?: string };
type RankingReference = { lane_rankings: { lane: string; rank: number; population: number }[]; sector_label?: string; sector_position?: number; sector_candidates: number; outranked_count?: number; required_peers?: PeerFact[]; sector_overview?: SectorOverview | null; market?: { median_return10?: number; up_fraction?: number } };
type Decision = { status: string; decision_id?: string; as_of_date?: string; market_assessment?: string; notice?: string; recommended?: Pick[]; coverage?: { candidates: number; reviewed: number; missing: string[]; errors?: Record<string,string> }; reviewed?: (Pick & { decision: string })[] };
const props = defineProps<{ value?: unknown }>();
const decision = computed(() => props.value as Decision | undefined);
const stageNames: Record<string,string> = { accumulation: '横盘潜伏', initial_breakout: '初步启动', strong_pullback: '强势回踩', post_limit: '涨停后承接', other: '其他观察' };
const pct = (v?: number | null) => (typeof v === 'number' ? `${v.toFixed(1)}%` : '—');
const share = (v?: number | null) => (typeof v === 'number' ? `${(v * 100).toFixed(0)}%` : '—');
const yi = (v?: number | null) => (typeof v === 'number' ? `${(v / 1e8).toFixed(2)}亿` : '—');
const peerFact = (r: RankingReference, symbol: string) => (r.required_peers || []).find(p => p.symbol === symbol);
const peerPosition = (p?: PeerFact) => {
  if (!p) return '—';
  const lane = `${p.best_lane} 第${p.best_rank}/${p.best_population}`;
  return p.scope === 'global' ? `跨板块补位 · ${p.sector_label || '未知板块'} · ${lane}` : `板块第${p.sector_position} · ${lane}`;
};
const peerDuty = (p?: PeerFact) => (p ? (p.scope === 'global' ? '补位' : '是') : '否');
// The whole sector, not just this scan's candidates; when the system has no
// aggregate the author's ETF proxy is shown in its place.
const sectorOverviewText = (r: RankingReference, note: Note) => {
  const o = r.sector_overview;
  if (!o) {
    const proxy = note.sector_view?.proxy;
    return proxy?.kind === 'etf'
      ? `系统无该板块聚合，使用 ETF 代理 ${proxy.name}（${proxy.symbol}）`
      : '系统无该板块聚合，本轮也没有登记 ETF 代理';
  }
  return `${o.label}（全市场 ${o.members} 只成分）10日涨幅中位 ${pct(o.return10_median)} vs 全市场 ${pct(r.market?.median_return10)}，`
    + `上涨占比 ${share(o.up_fraction)}，近3日广度 ${share(o.recent_breadth)}（较前期 ${share(o.breadth_acceleration)}），`
    + `3日资金 ${yi(o.flow_3d)}，系统判定 ${o.relative_strength}（参考标签，不是评分）`;
};
const download = () => {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(decision.value, null, 2)], { type: 'application/json' }));
  link.download = `recommendation-${decision.value?.as_of_date}.json`; link.click(); URL.revokeObjectURL(link.href);
};
</script>
<template>
  <section class="recommendation-decision" aria-label="正式推荐决策" :data-decision-id="decision?.decision_id">
    <header><div><h2>本轮重点与观察</h2><p>与持仓独立 · 来自同一份正式决策，不按聊天记录拼名单</p></div><button v-if="decision?.decision_id" @click="download">下载决策凭据</button></header>
    <p v-if="decision?.status !== 'ready'" role="status" class="warning">{{ decision?.notice || '本轮推荐决策尚未发布；下面的策略名单仅为扫描候选。' }}</p>
    <template v-if="decision?.decision_id">
      <p v-if="decision.coverage?.missing?.length" class="warning">本轮必核缺项：{{ decision.coverage.missing.join('、') }}。有效研究仍展示，不能据此覆盖完整推荐组。</p>
      <p v-for="(reason, symbol) in decision.coverage?.errors" :key="symbol" class="warning">{{ symbol }}：{{ reason }}</p>
      <p>{{ decision.market_assessment }}</p>
      <p class="meta">{{ decision.as_of_date }} · {{ decision.status }} · 完整候选 {{ decision.coverage?.candidates }}，完成深入复核 {{ decision.coverage?.reviewed }} · 编号 {{ decision.decision_id.slice(0, 12) }}</p>
      <div class="picks"><article v-for="p in decision.recommended" :key="p.symbol">
        <h3>{{ p.priority }}. {{ p.name }}（{{ p.symbol.split('.')[0] }}）</h3><span class="stage">{{ stageNames[p.stage] || p.stage }}</span>
        <section class="company-brief" aria-label="公司与基本面">
          <h4>公司与基本面</h4>
          <p><strong>所属行业：</strong>{{ p.sector || '本轮未登记' }}</p>
          <p><strong>主营与经营：</strong>{{ p.business || '本轮正式研究缺少主营与经营说明' }}</p>
        </section>
        <p>{{ p.why_now }}</p><p><strong>为什么优先：</strong>{{ p.comparison }}</p>
        <p><strong>观察触发：</strong>{{ p.trigger }}</p><p><strong>取消条件：</strong>{{ p.invalidation }}</p>
        <details><summary>公司风险</summary><p>{{ p.company_risk }}</p></details>
        <DisciplinePoolCard :symbol="p.symbol" :name="p.name" />
        <details v-if="p.recommendation_note && p.ranking_reference"><summary>推荐说明</summary>
          <p><strong>系统排名：</strong>{{ p.ranking_reference.lane_rankings.map(r => `${r.lane} 第${r.rank}/${r.population}`).join('；') || '本轮九策略均未入选' }}<template v-if="p.ranking_reference.sector_position">；{{ p.ranking_reference.sector_label }} 候选第{{ p.ranking_reference.sector_position }}/{{ p.ranking_reference.sector_candidates }}</template></p>
          <p v-if="p.ranking_reference.outranked_count"><strong>同板块有 {{ p.ranking_reference.outranked_count }} 只候选排在它前面。</strong></p>
          <p><strong>板块整体：</strong>{{ sectorOverviewText(p.ranking_reference, p.recommendation_note) }}</p>
          <template v-if="p.recommendation_note.sector_view">
            <p><strong>板块判断（作者）：</strong>{{ p.recommendation_note.sector_view.assessment }}；依据：{{ p.recommendation_note.sector_view.proxy?.kind === 'etf' ? `ETF 代理 ${p.recommendation_note.sector_view.proxy.name}（${p.recommendation_note.sector_view.proxy.symbol}）` : '系统板块成分聚合' }}<template v-if="p.recommendation_note.sector_view.proxy?.url">（<a :href="p.recommendation_note.sector_view.proxy.url" target="_blank" rel="noreferrer">{{ p.recommendation_note.sector_view.proxy.published_date }}</a>）</template></p>
            <p><strong>板块走势：</strong>{{ p.recommendation_note.sector_view.trend }}</p>
            <p><strong>板块量价：</strong>{{ p.recommendation_note.sector_view.volume_price }}</p>
          </template>
          <p v-if="p.recommendation_note.weak_sector_entry_reason"><strong>弱板块仍入选：</strong>{{ p.recommendation_note.weak_sector_entry_reason }}</p>
          <p v-if="p.recommendation_note.sector_disagreement_reason"><strong>与系统判定不一致：</strong>{{ p.recommendation_note.sector_disagreement_reason }}</p>
          <p><strong>如何使用排名：</strong>{{ p.recommendation_note.rank_assessment }}</p>
          <p><strong>最终入选理由：</strong>{{ p.recommendation_note.entry_reason }}</p>
          <p><strong>优先级理由：</strong>{{ p.recommendation_note.priority_reason }}</p>
          <p v-if="p.recommendation_note.carry_over_reason"><strong>旧推荐续留：</strong>{{ p.recommendation_note.carry_over_reason }}</p>
          <table v-if="p.recommendation_note.sector_peers?.length" class="peers">
            <thead><tr><th>同板块对手</th><th>系统位置</th><th>10日涨幅</th><th>5日净流入占比</th><th>必答</th><th>为什么不选</th></tr></thead>
            <tbody><tr v-for="peer in p.recommendation_note.sector_peers" :key="peer.symbol">
              <td>{{ peerFact(p.ranking_reference, peer.symbol)?.name || peer.symbol }}（{{ peer.symbol.split('.')[0] }}）</td>
              <td>{{ peerPosition(peerFact(p.ranking_reference, peer.symbol)) }}</td>
              <td>{{ pct(peerFact(p.ranking_reference, peer.symbol)?.metrics?.return_10d) }}</td>
              <td>{{ pct(peerFact(p.ranking_reference, peer.symbol)?.metrics?.net5_amount_pct) }}</td>
              <td>{{ peerDuty(peerFact(p.ranking_reference, peer.symbol)) }}</td>
              <td>{{ peer.why_not }}</td>
            </tr></tbody>
          </table>
          <p v-for="check in p.recommendation_note.information_checks" :key="check.url + check.topic"><strong>{{ check.topic }}：</strong>{{ check.finding }}（<a :href="check.url" target="_blank" rel="noreferrer">{{ check.published_date }}</a>）</p>
        </details>
      </article></div>
      <details><summary>其他完成复核的股票与取舍</summary><p v-for="p in decision.reviewed?.filter(x => x.decision !== 'recommend')" :key="p.symbol"><strong>{{ p.name }}：</strong>{{ p.comparison }}</p></details>
      <p class="meta">{{ decision.notice }}</p>
    </template>
  </section>
</template>
<style scoped>
.recommendation-decision{margin:0 0 24px;padding:22px;border:1px solid #d8e2ee;border-radius:12px;background:#f8fbff;color:#203149}.recommendation-decision header{display:flex;justify-content:space-between;gap:18px;align-items:start}.recommendation-decision h2{margin:0}.recommendation-decision p{line-height:1.7}.meta,header p{color:#61738a;font-size:13px;overflow-wrap:anywhere}.picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:14px}.picks article{padding:18px;background:var(--gs-paper);border:1px solid var(--gs-line);border-radius:9px}.picks h3{margin:0 0 12px}.stage{font-size:12px;background:#e7efff;color:#234c92;padding:4px 8px;border-radius:5px}.company-brief{margin:14px 0;padding:12px 14px;border-left:3px solid #4e73b8;border-radius:5px;background:#f2f6fc}.company-brief h4{margin:0 0 6px;font-size:14px}.company-brief p{margin:5px 0}.warning{background:#fff1d9;padding:12px;color:#8b5010}.recommendation-decision button{padding:7px 12px;white-space:nowrap;border:1px solid #c6d3e4;background:var(--gs-paper);border-radius:5px;cursor:pointer}details{margin-top:14px}summary{cursor:pointer}table.peers{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}table.peers th,table.peers td{border:1px solid var(--gs-line);padding:5px 7px;text-align:left;vertical-align:top}table.peers th{background:#eef4fb;white-space:nowrap}@media(max-width:600px){.recommendation-decision{padding:14px}.recommendation-decision header{display:block}}
</style>

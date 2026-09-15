<script setup lang="ts">
import { computed } from 'vue';
export type EventResearch = {
  status: string; run_id?: string; cutoff?: string; summary?: string; stale?: boolean;
  published_at?: string;
  analysis?: {status:string; failure_stage?:string; failure_code?:string; repair_count?:number};
  schedule?: {last_expected_at: string|null; next_expected_at: string|null; state: string;
    calendar?: {status:string; coverage_end?:string; reason?:string}};
  coverage?: { documents: number; review_input: number; reviewed: number };
  events: { event_id: string; fact: string; expectation: string; surprise: string; transmission: string;
    horizon: string; action: string; counterevidence: string; invalidate: string; change: string;
    symbols: { name: string; symbol: string; relation: string; direction: string }[];
    sources: { source: string; url: string; published_at: string; document_id: string }[] }[];
  leads: { document_id: string; title: string; published_at: string; symbols: { name: string; symbol: string }[] }[];
};
const props=defineProps<{ value?: EventResearch; symbols?: string[] }>();
const events=computed(()=>props.value?.events.filter(e=>!props.symbols || e.symbols.length===0 || e.symbols.some(s=>props.symbols?.includes(s.symbol)))??[]);
const leads=computed(()=>(props.value?.leads??[]).filter(e=>!props.symbols || e.symbols.some(s=>props.symbols?.includes(s.symbol))).slice(0,12));
const labels:Record<string,string>={analyzed:'已完成有界消息研究',leads_only:'线索已获取 · 语义研究未完成',failed:'消息链失败',absent:'没有可用消息快照',no_news:'来源没有返回消息'};
const surprise:Record<string,string>={positive:'正向预期差',negative:'负向预期差',neutral:'符合预期',unknown:'预期差未知'};
const failureStages:Record<string,string>={connection:'连接阶段',http:'HTTP请求',generation:'模型生成',response_format:'JSON解析',citation_validation:'引用校验',citation_repair:'引用复核',content_validation:'分析内容校验'};
const time=(value?:string|null)=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知';
</script>
<template>
  <section v-if="value" class="events-panel" data-testid="event-research">
    <header><h3>消息变化与方向影响</h3><span>{{ labels[value.status]??value.status }}</span></header>
    <p v-if="value.schedule" class="delivery">交易日 09:00 / 12:00 / 22:00 · 休市衔接：最后交易日 22:00、复市前一日 22:00、复市日 09:00<br>
      实际发布 {{ time(value.published_at) }} · 下一目标 {{ time(value.schedule.next_expected_at) }}（北京时间）</p>
    <p v-if="value.schedule?.state==='calendar_unavailable' || ['missing','expiring'].includes(value.schedule?.calendar?.status??'')" role="alert">
      交易日历需要核验更新：{{ value.schedule?.calendar?.reason }}；未核验日期不按工作日猜测开市。
    </p>
    <p class="summary">{{ value.summary }}</p>
    <p v-if="value.analysis?.status==='failed'" role="alert">失败位置：{{ failureStages[value.analysis.failure_stage??'']??'旧回执未细分' }}；错误码：{{ value.analysis.failure_code??'未记录' }}。内容校验失败不等于模型无法连接。</p>
    <p v-if="value.stale" role="alert">消息快照已过期或本次刷新失败，仅作历史背景，不冒充本轮新消息。<span v-if="value.schedule"> 应更新时点：{{ time(value.schedule.last_expected_at) }}。</span></p>
    <article v-for="event in events" :key="event.event_id">
      <h4>{{ event.fact }}</h4><p>{{ event.transmission }}</p>
      <dl><dt>预期与时间</dt><dd>{{ event.expectation }} · {{ surprise[event.surprise] }} · {{ event.horizon }}</dd>
        <dt>观察处理</dt><dd>{{ event.action }}</dd><dt>反证与失效</dt><dd>{{ event.counterevidence }}；{{ event.invalidate }}</dd></dl>
      <p v-for="stock in event.symbols" :key="stock.symbol">{{ stock.name }}（{{ stock.symbol.split('.')[0] }}）：{{ stock.relation }}</p>
      <footer v-for="source in event.sources" :key="source.document_id">
        <a v-if="/^https?:\/\//.test(source.url)" :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.source }}</a><span v-else>{{ source.source }} · 未提供原文链接</span> · {{ time(source.published_at) }}
      </footer>
    </article>
    <details v-if="leads.length"><summary>快讯线索附录（非已核查利好）</summary>
      <p v-for="lead in leads" :key="lead.document_id">{{ lead.title }}<small> {{ time(lead.published_at) }}</small></p>
    </details>
    <footer>消息截止 {{ time(value.cutoff) }}（北京时间） · 轮次 {{ value.run_id?.slice(0,8)??'无' }}<br>
      有界快讯窗口，不代表全部消息。研究不自动改变策略权重，不构成买入授权。</footer>
  </section>
</template>
<style scoped>
.events-panel{margin:18px 0;padding:20px;border:1px solid var(--el-border-color);border-radius:12px;background:var(--el-bg-color)}header{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap}h3,h4{margin:0}header span,footer,small{color:var(--el-text-color-secondary);font-size:12px}p,dd{line-height:1.8;font-size:14px}article{border-top:1px solid var(--el-border-color-lighter);padding:18px 0}dt{font-size:12px;color:var(--el-text-color-secondary)}dd{margin:3px 0 10px}details{margin:16px 0}summary{cursor:pointer;font-size:13px}footer{line-height:1.7}a{color:var(--el-color-primary)}
</style>

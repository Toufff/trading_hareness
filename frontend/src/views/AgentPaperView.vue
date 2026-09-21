<script setup lang="ts">
import {computed,ref,onMounted,onBeforeUnmount} from 'vue';
import {getJson} from '../api/http';
import WorkspaceHeader from '../components/WorkspaceHeader.vue';
type Daily={trading_date:string;agent_equity:number;agent_return_pct:number|null;human_equity:number|null;human_return_pct:number|null;human_basis?:string;human_comparable?:boolean;human_fills_imported_through:string|null;human_missing_prices:string[]};
type Order={placed_at:string;symbol:string;name:string|null;side:string;order_type:string;quantity:number;limit_price:string|null;status:string;filled_quantity:number;fill_price:string|null;fees:string;reason:string|null;reject_reasons:string[]};
type Decision={decided_at:string;status:string;market_view:string|null;notes:string|null;order_count:number|null;error:string|null;duration_ms:number|null};
type Position={symbol:string;name:string|null;quantity:number;sellable_quantity:number;average_cost:string;realized_pnl:string};
type Status={status:string;account_key:string;model?:string;start_date?:string;initial_equity?:number;cash?:number;day?:string;
  latest_nav?:{as_of:string;equity:string;return_pct:number|null;price_basis:string}|null;daily?:Daily[];positions?:Position[];orders?:Order[];
  decisions_today?:{decided:number;failed:number};recent_decisions?:Decision[];comparison_note?:string};
const data=ref<Status|null>(null),error=ref(''),loading=ref(false);
let ctrl:AbortController|undefined;let timer:number|undefined;
const sides:Record<string,string>={buy:'买',sell:'卖'};
const states:Record<string,string>={filled:'成交',partially_filled:'部分成交',open:'挂单中',rejected:'拒绝',cancelled:'已撤',expired:'收盘未成交'};
const time=(s:string|undefined|null)=>s?new Date(s).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const pct=(n:number|null|undefined)=>n==null?'—':(n>0?'+':'')+n.toFixed(2)+'%';
const kind=(o:Order)=>o.order_type==='limit'?' 限'+o.limit_price:' 市价';
const latestDaily=computed(()=>data.value?.daily?.at(-1));
async function load(){
  ctrl?.abort();ctrl=new AbortController();loading.value=true;error.value='';
  try{data.value=await getJson<Status>('/api/research/agent-paper/status',{signal:ctrl.signal});}
  catch(e){if((e as Error).name!=='AbortError')error.value=(e as Error).message;}finally{loading.value=false;}
}
onMounted(()=>{void load();timer=window.setInterval(()=>void load(),60000);});
onBeforeUnmount(()=>{ctrl?.abort();if(timer)window.clearInterval(timer);});
</script>
<template>
<WorkspaceHeader active="agent-paper" />
<main class="agent-page">
  <header><div><small>纸上演练 · 不连接券商</small><h1>模拟盘与实盘对照</h1><p v-if="data?.model">{{data.model}} · 起始 {{data.start_date}} · 初始权益 {{data.initial_equity}}</p></div>
    <button @click="load()" :disabled="loading">{{loading?'读取中…':'刷新'}}</button></header>
  <p v-if="error" role="alert" class="error">{{error}}</p>
  <p v-if="data?.status==='not_configured'">模拟账户 {{data.account_key}} 尚未初始化。</p>
  <template v-if="data?.status==='ok'">
    <div class="metrics">
      <div><small>AI 最新权益</small><strong>{{data.latest_nav?.equity??'—'}}</strong><em>{{pct(data.latest_nav?.return_pct)}} · {{time(data.latest_nav?.as_of)}}</em></div>
      <div><small>人类重建权益（{{latestDaily?.trading_date??'—'}} 收盘）</small><strong>{{latestDaily?.human_equity??'—'}}</strong><em>{{pct(latestDaily?.human_return_pct)}}</em></div>
      <div><small>今日决策 成功 / 失败</small><strong>{{data.decisions_today?.decided}} / {{data.decisions_today?.failed}}</strong></div>
      <div><small>现金</small><strong>{{data.cash}}</strong></div>
    </div>
    <p class="note">{{data.comparison_note}}<span v-if="latestDaily"> 成交导入至 {{latestDaily.human_fills_imported_through??'无'}}。</span></p>
    <section><h2>逐日对比</h2><table><thead><tr><th>日期</th><th>AI 权益</th><th>AI 收益</th><th>人类权益</th><th>人类收益</th></tr></thead>
      <tbody><tr v-for="d in data.daily" :key="d.trading_date"><td>{{d.trading_date}}</td><td>{{d.agent_equity}}</td><td>{{pct(d.agent_return_pct)}}</td><td>{{d.human_equity??'—'}}</td><td>{{pct(d.human_return_pct)}}</td></tr></tbody></table></section>
    <section><h2>AI 持仓</h2><table><thead><tr><th>代码</th><th>名称</th><th>数量</th><th>可卖</th><th>成本</th><th>已实现</th></tr></thead>
      <tbody><tr v-for="p in data.positions" :key="p.symbol"><td>{{p.symbol}}</td><td>{{p.name}}</td><td>{{p.quantity}}</td><td>{{p.sellable_quantity}}</td><td>{{Number(p.average_cost).toFixed(3)}}</td><td>{{Number(p.realized_pnl).toFixed(2)}}</td></tr></tbody></table></section>
    <section><h2>{{data.day}} 委托</h2><p v-if="!data.orders?.length" class="note">今天还没有委托。</p>
      <table v-else><thead><tr><th>时间</th><th>股票</th><th>方向</th><th>数量</th><th>状态</th><th>成交价</th><th>依据</th></tr></thead>
      <tbody><tr v-for="o in data.orders" :key="o.placed_at+o.symbol"><td>{{time(o.placed_at)}}</td><td>{{o.name??''}} {{o.symbol}}</td><td>{{sides[o.side]??o.side}}{{kind(o)}}</td>
        <td>{{o.filled_quantity}}/{{o.quantity}}</td><td>{{states[o.status]??o.status}}<small v-if="o.reject_reasons?.length"> {{o.reject_reasons.join(',')}}</small></td><td>{{o.fill_price?Number(o.fill_price).toFixed(3):'—'}}</td><td class="reason">{{o.reason}}</td></tr></tbody></table></section>
    <section><h2>最近决策</h2><article v-for="d in data.recent_decisions" :key="d.decided_at" :class="{failed:d.status!=='decided'}">
      <b>{{time(d.decided_at)}}</b> <span v-if="d.status!=='decided'">失败：{{d.error}}</span><span v-else>{{d.market_view}} · {{d.order_count}} 笔委托</span>
      <p v-if="d.notes" class="note">{{d.notes}}</p></article></section>
  </template>
</main>
</template>
<style scoped>
.agent-page{max-width:1200px;margin:auto;padding:24px 16px;color:var(--gs-ink);font:14px/1.6 'Segoe UI','Microsoft YaHei',sans-serif;box-sizing:border-box}
header{display:flex;justify-content:space-between;align-items:center;gap:12px}h1{font-size:26px;margin:2px 0}header small{letter-spacing:1px;color:var(--gs-muted)}header p,.note{color:var(--gs-muted)}
button{font:inherit;border:1px solid var(--gs-line);border-radius:7px;background:var(--gs-paper);padding:8px 12px;cursor:pointer}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.metrics div{padding:14px;background:var(--gs-paper);border:1px solid var(--gs-line);border-radius:9px;min-width:0}
.metrics small,.metrics strong,.metrics em{display:block}.metrics small{color:var(--gs-muted)}.metrics strong{font-size:18px;margin-top:6px}.metrics em{font-style:normal;color:var(--gs-muted)}
section{background:var(--gs-paper);border:1px solid var(--gs-line);border-radius:10px;padding:14px;margin-top:14px;overflow-x:auto}h2{font-size:16px;margin:0 0 8px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--gs-line);white-space:nowrap}td.reason{white-space:normal;min-width:260px}
article{border-bottom:1px solid var(--gs-line);padding:8px 0;overflow-wrap:anywhere}article.failed{color:var(--gs-up)}.error{background:var(--el-color-danger-light-9);color:var(--gs-up);padding:12px}
@media(max-width:800px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
header > div { min-width:0; }
header button { flex-shrink:0; white-space:nowrap; }
.metrics strong { overflow-wrap:anywhere; }
</style>

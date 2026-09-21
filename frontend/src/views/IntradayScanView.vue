<script setup lang="ts">
import {computed,ref,onMounted,onBeforeUnmount,watch,defineAsyncComponent} from 'vue';
import {getJson} from '../api/http';
import type {ScanResponse,ScanItem} from '../types/intraday-scan';
import type {EChartsOption} from 'echarts';
import EventResearchPanel from '../components/EventResearchPanel.vue';
import WorkspaceHeader from '../components/WorkspaceHeader.vue';
const Chart=defineAsyncComponent(()=>import('../components/IntradayScanChart.vue'));
const data=ref<ScanResponse|null>(null),detail=ref<ScanItem|null>(null),changes=ref<ScanResponse['changes']>([]);
const loading=ref(false),error=ref(''),detailError=ref(''),laneKey=ref(''),symbol=ref(''),query=ref(''),mode=ref('minute');
let ctrl:AbortController|undefined; let detailCtrl:AbortController|undefined;
const lane=computed(()=>data.value?.result?.lanes.find(l=>l.key===laneKey.value));
const coverageGaps=computed(()=>Object.entries(lane.value?.data_gaps??{}).filter(([,n])=>n>0));
const items=computed(()=>lane.value?.items.filter(r=>(r.name+r.symbol).includes(query.value.trim()))??[]);
const labels:Record<string,string>={confirmed_observation:'结构转强',platform_observation:'平台内潜伏',wait_confirmation:'等待承接',invalidated:'结构转弱',data_gap:'分钟证据缺口',execution_uncertain:'接近涨停/成交未证实'};
const fmt=(n:number|undefined,d=2)=>n==null?'—':n.toFixed(d);
const displayTime=(s:string|undefined)=>s?new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'—';
const planLabels:Record<string,string>={armed:'等待后续条件',triggered_unverified_fill:'价格条件曾触发，未证实成交',invalidated:'原计划失效',after_close_watch_only:'闭市观察，不生成当日买单'};
async function load(run?:string){
  ctrl?.abort();ctrl=new AbortController();loading.value=true;error.value='';
  try{const value=await getJson<ScanResponse>('/api/research/intraday-scans'+(run?'?run_id='+run:''),{signal:ctrl.signal});
    data.value=value;laneKey.value=value.result?.lanes[0]?.key??'';symbol.value='';detail.value=null;
    await pick(value.result?.lanes[0]?.items[0]?.symbol??'');
  }catch(e){if((e as Error).name!=='AbortError')error.value=(e as Error).message;}finally{loading.value=false;}
}
async function pick(code:string){
  detailCtrl?.abort();detailCtrl=new AbortController();symbol.value=code;detail.value=null;changes.value=[];detailError.value='';
  if(!code||!data.value?.run_id)return;
  try{const value=await getJson<ScanResponse>(`/api/research/intraday-scans?run_id=${data.value.run_id}&lane=${laneKey.value}&symbol=${code}`,{signal:detailCtrl.signal});
    detail.value=value.detail??null;changes.value=value.changes;
  }catch(e){if((e as Error).name!=='AbortError')detailError.value=(e as Error).message;}
}
watch(laneKey,()=>{query.value='';void pick(lane.value?.items[0]?.symbol??'');});
onMounted(()=>void load());onBeforeUnmount(()=>{ctrl?.abort();detailCtrl?.abort();});
const option=computed<EChartsOption>(()=>{
  const d=detail.value;const bars=mode.value==='minute'?d?.chart??[]:d?.ohlc??[];
  const marks=[{name:'结构参考',value:d?.reference},{name:'失效参考',value:d?.support}].filter(m=>m.value!=null).map(m=>({name:m.name,yAxis:m.value,label:{formatter:m.name+' '+fmt(m.value)}}));
  return {animationDuration:250,tooltip:{trigger:'axis'},grid:{left:55,right:85,top:30,bottom:55},
    xAxis:{type:'category',data:bars.map(b=>'time'in b?b.time:b.date)},yAxis:{type:'value',scale:true},
    dataZoom:[{type:'inside'},{type:'slider',height:16,bottom:5}],
    series:mode.value==='minute'?[{name:'成交采样价',type:'line',showSymbol:false,data:d?.chart?.map(b=>b.close),markLine:{symbol:'none',data:marks}},
      {name:'VWAP',type:'line',showSymbol:false,data:d?.chart?.map(b=>b.vwap),lineStyle:{color:'#d99432',width:1.5}}]:
      [{name:'真实日K',type:'candlestick',data:d?.ohlc?.map(b=>[b.open,b.close,b.low,b.high]),itemStyle:{color:'#d85858',color0:'#258c77',borderColor:'#d85858',borderColor0:'#258c77'},markLine:{symbol:'none',data:marks}}]};
});
</script>
<template>
<WorkspaceHeader active="intraday" />
<main class="scan-page" :data-run-id="data?.run_id??''">
  <header><div><small>盘中札记</small><h1>盘中策略观察</h1><p>九策略独立扫描 · 前瞻计划 · 原候选连续跟踪</p></div><a href="/research">返回研究台</a></header>
  <section class="toolbar"><button @click="load()" :disabled="loading">{{loading?'正在读取…':'刷新结果'}}</button>
    <select aria-label="运行记录" :value="data?.run_id" @change="load(($event.target as HTMLSelectElement).value)">
      <option v-for="r in data?.runs.filter(r=>r.state==='completed')" :key="r.run_id" :value="r.run_id">{{r.cutoff}} · {{r.model_version}}</option>
    </select><span v-if="data?.result">{{data.result.phase==='after_close_initialization'?'闭市初始化，不是收盘前建议':'盘中暂定结果'}}</span>
  </section>
  <p v-if="error" role="alert" class="error">{{error}}</p>
  <p v-if="data?.runs[0]?.state==='failed'" class="warning">最近一次扫描失败于 {{data.runs[0].stage}}，下方为保留的历史成功结果，并非最新成功。</p>
  <p v-if="data?.runs[0]?.stage==='completed_reconciliation_failed'" class="warning">本轮扫描已保存，但收盘对照失败；不能视为完整闭环。</p>
  <p v-if="data?.status==='no_run'">尚无九策略盘中扫描。刷新只读数据库，不会自动启动采集。</p>
  <template v-if="data?.result">
    <EventResearchPanel :value="data.result.event_research" />
    <div class="metrics"><div><small>市场窗口（北京时间）</small><strong :title="data.result.cutoff">{{displayTime(data.result.cutoff)}}</strong></div><div><small>上涨 / 下跌</small><strong>{{data.result.market.up}} / {{data.result.market.down}}</strong></div><div><small>样本 / 涨幅中位</small><strong>{{data.result.market.symbols}} / {{fmt(data.result.market.median)}}%</strong></div><div><small>严格日K / 分时覆盖</small><strong>{{data.result.history_health?.ready??'—'}} / {{data.result.minute_health?.received??'—'}}</strong></div></div>
    <p class="metadata">实际可知 {{data.result.observed_at}} · 日K采集 {{data.result.ohlc_captured_at??'未补充'}} · 运行 {{data.run_id}}</p>
    <nav aria-label="策略"><button v-for="l in data.result.lanes" :key="l.key" :class="{active:laneKey===l.key}" @click="laneKey=l.key">{{l.label}} <small>{{l.total_matches??'—'}}</small></button></nav>
    <p v-if="coverageGaps.length" class="warning" data-testid="coverage-warning">本策略部分覆盖：<span v-for="[key,n] in coverageGaps" :key="key">{{key==='strict_ohlc_40'?'缺40根完整日K':key}} {{n}}只；</span>未覆盖不等于不符合，零匹配不代表全市场没有机会。</p>
    <div class="workspace"><section class="list"><input v-model="query" aria-label="搜索股票" placeholder="搜索名称 / 代码"/><p>本轮匹配 {{lane?.total_matches??'—'}} · 含往期跟踪 {{lane?.items.length}} · {{coverageGaps.length?'部分覆盖':'已执行'}}</p>
      <button v-for="r in items" :key="r.symbol" :class="['stock',{selected:symbol===r.symbol}]" @click="pick(r.symbol)"><span><b>{{r.name}}</b><small>{{r.symbol}} · {{r.matched_today?'本轮匹配':'往期跟踪'}}</small></span><span>{{fmt(r.price)}}<small>{{labels[r.state]??r.state}}</small></span></button>
      <p v-if="!items.length">本策略没有候选；不从其他策略补票。</p>
    </section><section class="detail" :data-symbol="detail?.symbol??''">
      <p v-if="detailError" role="alert">{{detailError}}</p>
      <template v-if="detail"><h2>{{detail.name}} <small>{{detail.symbol}}</small></h2><p class="conclusion">{{detail.reason}}</p>
        <p v-if="detail.current_reason"><b>本轮筛选依据：</b>{{detail.current_reason}}</p>
        <p>{{detail.original_reason}}</p><div class="prices"><span>成交额 {{detail.amount==null?'—':fmt(detail.amount/1e8)}} 亿</span><span>主力口径净额 {{detail.main_net==null?'—':fmt(detail.main_net/1e8)}} 亿</span><span>参考 {{fmt(detail.reference)}} / 失效 {{fmt(detail.support)}}</span></div>
        <div class="chart-controls"><button @click="mode='minute'" :class="{active:mode==='minute'}">分时与VWAP</button><button @click="mode='daily'" :class="{active:mode==='daily'}">真实日K</button></div>
        <Chart v-if="mode==='minute'?detail.chart?.length:detail.ohlc?.length" :option="option" class="chart"/>
        <p v-else class="warning">该候选本轮没有对应图形证据，未补造数据。</p>
        <h3>入场情景与边界</h3><p>{{detail.entry_scenario??'历史版本未提供结构化情景'}}</p><p>{{detail.amount_basis}}</p>
        <p v-for="gap in detail.evidence_gaps" :key="gap" class="warning">{{gap}}</p>
        <p>计划记录：{{planLabels[detail.plan?.state??'']??'无'}} · {{displayTime(detail.plan?.created_at)}}。价格触发不证明成交，不是自动买入授权。</p>
        <h3>收盘对照</h3><p v-if="!changes?.length">尚无该股同日盘中—收盘对照。</p>
        <p v-for="c in changes" :key="c.source_cutoff">{{c.source_cutoff}} → 收盘：观察价变化 {{c.observation_change_pct==null?'—':fmt(c.observation_change_pct)}}%；{{labels[c.source_state]??c.source_state}} → {{labels[c.close_state]??c.close_state}}。不是实盘收益。</p>
      </template><p v-else-if="symbol">正在读取本轮股票详情…</p><p v-else>选择策略与候选查看证据。</p>
    </section></div>
    <footer>输入指纹 {{data.result.input_hash}}。独立于持仓；不混入旧持仓，不改券商自选，不交易。</footer>
  </template>
</main>
</template>
<style scoped>
.scan-page{max-width:1500px;margin:auto;padding:28px;color:var(--gs-ink);font:14px/1.6 'Segoe UI','Microsoft YaHei',sans-serif}header{display:flex;justify-content:space-between;align-items:center}h1{font-size:28px;margin:3px 0}header small{letter-spacing:2px;color:var(--gs-muted)}header p,.metadata,footer{color:var(--gs-muted)}a{color:var(--gs-sky);text-decoration:none}button,select,input{font:inherit;border:1px solid var(--gs-line);border-radius:7px;background:var(--gs-paper);color:inherit;padding:8px 12px}button{cursor:pointer}.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:20px 0}.toolbar select{max-width:550px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metrics div{padding:15px;background:var(--gs-paper);border:1px solid var(--gs-line);border-radius:9px}.metrics small,.metrics strong{display:block}.metrics small{color:var(--gs-muted)}.metrics strong{font-size:16px;margin-top:8px}nav{display:flex;gap:7px;flex-wrap:wrap;margin:20px 0}.active{background:#274d80;color:var(--gs-paper);border-color:#274d80}nav small{opacity:.75;margin-left:5px}.workspace{display:grid;grid-template-columns:325px minmax(0,1fr);gap:18px;align-items:start}.list,.detail{background:var(--gs-paper);border:1px solid var(--gs-line);border-radius:10px;padding:18px}.list{max-height:980px;overflow:auto}.list input{width:100%;box-sizing:border-box}.list p{color:var(--gs-muted);font-size:12px}.stock{width:100%;display:flex;justify-content:space-between;text-align:left;border:0;border-bottom:1px solid var(--gs-line);border-radius:0;padding:13px 6px}.stock small{display:block;font-size:11px;color:var(--gs-muted)}.stock.selected{background:var(--gs-wash);border-radius:6px}.stock>span:last-child{text-align:right}.detail h2{margin:0;font-size:22px}.detail h2 small{font-size:13px;color:var(--gs-muted);font-weight:400}.conclusion{font-size:16px;color:var(--gs-sky)}.prices{display:flex;gap:20px;flex-wrap:wrap;padding:12px 0;border-top:1px solid var(--gs-line)}.chart{height:340px;width:100%}.chart-controls{display:flex;gap:6px}.warning{background:var(--el-color-warning-light-9);color:var(--gs-warning);padding:10px;border-radius:5px}.error{background:var(--el-color-danger-light-9);color:var(--gs-up);padding:12px}footer{font-size:11px;margin-top:20px;overflow-wrap:anywhere}h3{font-size:15px;margin-bottom:5px}.metadata{font-size:12px;overflow-wrap:anywhere}@media(max-width:900px){.scan-page{padding:14px}.metrics{grid-template-columns:repeat(2,1fr)}.workspace{grid-template-columns:1fr}.list{max-height:320px}.toolbar select{max-width:100%}.detail{padding:12px}.chart{height:290px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
.metrics>div,.detail,.list{min-width:0}.metrics strong{overflow-wrap:anywhere}.detail p{overflow-wrap:anywhere}
@media(max-width:900px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.toolbar select{width:100%;min-width:0}}
</style>

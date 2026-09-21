<script setup lang="ts">
import { computed } from 'vue';
export type Effectiveness = { status: string; as_of_date: string; finding_count?: number; reason?: string; manual_notice?: string; page_total?: number;
  groups: { lane: string; profile: string; regime: string; source_kind: string; status: string; timing: string;
    independent_sessions: number; minimum_sessions: number; top_minus_rest_pp: number | null; finding: string | null;
    excluded_dates: number; overlapping_dates: number;
    shadow?: { label: string; test_sessions: number; test_delta_pp: number | null; missing_feature_dates: number }[] }[] };
const props=defineProps<{ value?: Effectiveness; lane?: string; lanes?: {key:string;label:string}[] }>();
const groups=computed(()=>props.value?.groups.filter(g=>!props.lane || g.lane===props.lane) ?? []);
const laneName=(key:string)=>props.lanes?.find(l=>l.key===key)?.label ?? key;
const regimeName=(key:string)=>({risk_off:'弱势环境',risk_on:'强势环境',neutral:'中性环境',unknown:'环境未标注',legacy_unknown:'旧记录未标注环境'}[key] ?? key);
const label=(status:string)=>({ exploratory:'历史探索', insufficient:'前瞻样本不足', sufficient:'样本可评估' }[status] ?? status);
const pct=(v:number|null)=>v==null?'暂无':`${v>0?'+':''}${v.toFixed(2)} 个百分点`;
</script>
<template>
  <details class="effectiveness" data-testid="strategy-effectiveness">
    <summary>策略效果反馈 <span>{{ !value ? '本轮未生成' : value.status!=='completed' ? '执行失败' : `${value.finding_count ?? 0} 项异常待复核` }}</span></summary>
    <p v-if="!value || value.status!=='completed'" role="alert">{{ value?.reason ?? '本轮没有效果评估，不能解释为策略有效。' }}</p>
    <template v-else>
      <p v-if="value.page_total != null">本策略范围已加载 {{ value.groups.length }} / {{ value.page_total }} 组；未加载不表示没有样本。</p>
      <p>截至 {{ value.as_of_date }}。按版本、市场状态、来源隔离，日期等权、剔除重叠窗口。样本不足不等于通过；历史补录不算前瞻验证。</p>
      <div class="effect-table"><table><thead><tr><th>策略 / 来源</th><th>评估状态</th><th>独立日期</th><th>前排减后排</th></tr></thead>
        <tbody><tr v-for="g in groups" :key="[g.lane,g.profile,g.regime,g.source_kind,g.timing].join(':')">
          <td>{{ laneName(g.lane) }} · {{ g.source_kind==='manual'?'人工建议':g.source_kind==='machine'?'扫描':'旧记录' }}<small>版本 {{ g.profile.slice(0,8) }} · {{ regimeName(g.regime) }}</small></td>
          <td>{{ g.finding ? '前排持续落后，已提交复核' : label(g.status) }}</td><td>{{ g.independent_sessions }} / {{ g.minimum_sessions }}</td><td>{{ pct(g.top_minus_rest_pp) }}</td>
        </tr></tbody></table></div>
      <p>此处是观察效果，不是实盘收益。注册影子实验另做次日成交、费用与样本外对照；任何变更都必须由你最后批准。</p>
      <p>{{ value.manual_notice }}</p>
    </template>
  </details>
</template>
<style scoped>
.effectiveness{margin:18px 0;padding:16px;border:1px solid var(--el-border-color);border-radius:10px;background:var(--el-fill-color-extra-light)}
summary{cursor:pointer;font-weight:600;display:flex;gap:14px;flex-wrap:wrap}summary span{font-weight:400;color:var(--el-text-color-secondary)}
p{font-size:13px;line-height:1.7;color:var(--el-text-color-secondary)}.effect-table{overflow:auto;max-height:350px}table{width:100%;border-collapse:collapse;font-size:13px;text-align:left}th,td{padding:10px;border-bottom:1px solid var(--el-border-color-lighter);white-space:nowrap}small{display:block;color:var(--el-text-color-secondary);padding-top:4px}
</style>

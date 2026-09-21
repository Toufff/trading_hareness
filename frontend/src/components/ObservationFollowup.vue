<script setup lang="ts">
import { computed, ref } from 'vue';
export type FollowupItem = {
  origin_id: string; symbol: string; name: string; signal_date: string; lane: string; lane_label?: string;
  display_rank: number | null; rank: number; status: string; timing: string; expected_sessions: number;
  latest_return_pct: number | null; path_check: string; original_confirmation: string; original_invalidation: string;
  original_analysis?: string | null;
  virtual_entry?: {state: string; execution?: {net_return_pct?: number; status?: string}};
  windows: Record<string, { status: string; return_pct: number | null }>;
};
export type Followup = { status: string; note: string; total: number; items: FollowupItem[] };
const props = defineProps<{ followup?: Followup; lane?: string }>();
const query = ref(''); const all = ref(false); const count = ref(20);
const items = computed(() => (props.followup?.items ?? []).filter(r =>
  r.expected_sessions > 0 && (!props.lane || r.lane === props.lane) &&
  (all.value || r.display_rank != null) && `${r.name}${r.symbol}`.includes(query.value.trim())));
const labels: Record<string,string> = { both_touched_order_unknown:'上下沿都触及，先后未知', reference_low_broken:'原下沿曾跌破', reference_high_touched:'原上沿曾触及', no_reference_touch:'未触及原上下沿', no_data:'缺后续行情' };
const entryLabels: Record<string,string> = {unregistered:'未事前登记，不补造买点',waiting:'等待日线条件',expired:'观察期已到',invalidated:'结构已失效',data_gap:'证据有缺口',awaiting_next_session:'已确认，等待次一交易日',execution_window_pending:'模拟观察窗未结束',execution_blocked:'未通过可成交性检查',simulated:'日线代理模拟完成'};
const value = (w: FollowupItem['windows'][string] | undefined) => w?.status === 'observed' && w.return_pct != null ? `${w.return_pct > 0 ? '+' : ''}${w.return_pct.toFixed(2)}%` : w?.status === 'not_due' ? '未到期' : '缺数据';
</script>

<template>
  <section class="followup" data-observation-followup>
    <h3>往期候选 · 后续验证</h3>
    <p v-if="!followup">这轮尚无跟踪账本，不能据此判断旧推荐成功或失败。</p>
    <template v-else>
      <p>{{ followup.note }}</p>
      <p v-if="followup.status !== 'completed'" role="status">跟踪状态：{{ followup.status }}，与今日扫描结果分别核验。</p>
      <div class="controls"><input v-model="query" aria-label="搜索往期股票" placeholder="股票名称或代码" /><label><input v-model="all" type="checkbox" /> 包含非首屏候选</label><span>{{ items.length }}条记录（不同版本独立保留）</span></div>
      <div class="scroll"><table><thead><tr><th>股票 / 发现日</th><th>原策略 / 展示位</th><th>1日</th><th>3日</th><th>5日</th><th>10日</th><th>原结构检查</th></tr></thead><tbody>
        <tr v-for="r in items.slice(0,count)" :key="r.origin_id">
          <td><strong>{{ r.name }}（{{ r.symbol.split('.')[0] }}）</strong><small>{{ r.signal_date }} · {{ r.timing === 'reconstructed' ? '历史补录' : '前瞻记录' }}</small></td>
          <td>{{ r.lane_label ?? r.lane }} / {{ r.display_rank ?? '未进首屏' }}</td>
          <td v-for="h in ['1','3','5','10']" :key="h">{{ value(r.windows[h]) }}</td>
          <td><details><summary>{{ labels[r.path_check] ?? r.path_check }}</summary><p>原确认：{{ r.original_confirmation }}</p><p>原失效：{{ r.original_invalidation }}</p><p v-if="r.original_analysis">当时公司复核：{{ r.original_analysis }}</p><p v-if="r.virtual_entry">独立买点实验：{{ entryLabels[r.virtual_entry.state] ?? '待核查' }}<span v-if="r.virtual_entry.execution?.net_return_pct != null"> · 扣费模拟 {{ r.virtual_entry.execution.net_return_pct.toFixed(2) }}%</span></p><p>日线代理实验不是原文字条件的完整确认，也不代表实际成交。</p></details></td>
        </tr>
      </tbody></table></div>
      <p v-if="!items.length">当前筛选没有到期记录。</p><button v-if="items.length>count" @click="count+=40">显示更多记录</button>
      <p class="boundary">涨跌幅以发现日收盘为基准，不是可成交收益。涨停不算已买入，跌停不假定能卖出；未到期、缺数据、历史补录分别保留。</p>
    </template>
  </section>
</template>

<style scoped>
.followup{margin:20px 0;padding:18px;border:1px solid var(--el-border-color-lighter);border-radius:10px}.followup h3{margin:0 0 12px}.followup p{font-size:13px;line-height:1.7;color:var(--el-text-color-secondary)}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:12px;margin:12px 0}.controls>input{padding:8px;border:1px solid var(--el-border-color);border-radius:6px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--el-border-color-lighter);white-space:nowrap}small{display:block;color:var(--el-text-color-secondary);margin-top:6px}details p{white-space:normal;min-width:220px;max-width:420px}summary{cursor:pointer}.boundary{margin-bottom:0}button{padding:7px 14px;margin-top:12px;cursor:pointer}
</style>

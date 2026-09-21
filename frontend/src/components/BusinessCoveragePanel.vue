<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';
import { getJson } from '../api/http';
type Check = { key: string; label: string; status: string; reason: string; evidence: Record<string, any> };
const report = ref<{ status: string; checked_at: string; checks: Check[] } | null>(null);
const loading = ref(false);
const error = ref('');
let request: AbortController | null = null;
async function load() {
  request?.abort();
  const controller = request = new AbortController();
  loading.value = true; error.value = '';
  try { report.value = await getJson('/api/research/strategy/business-coverage', { signal: controller.signal }); }
  catch (cause) { if (!controller.signal.aborted) error.value = `读取失败：${String(cause)}`; }
  finally { if (request === controller) loading.value = false; }
}
onBeforeUnmount(() => request?.abort());
function evidence(check: Check): string {
  const e = check.evidence;
  if (check.key === 'discipline') return Object.entries(e.coverage ?? {}).map(([key, value]) => {
    const v = value as { covered: number; expected: number; missing_symbols: string[] };
    return `${key === 'holdings' ? '持仓' : '推荐'}：${v.covered}/${v.expected}；缺少有效线：${v.missing_symbols?.join('、') || '无'}`;
  }).join('。');
  if (check.key === 'equities') return `${e.date ?? '未知日期'} · ${e.stocks ?? 0} 只；近期基准 ${e.baseline_stocks ?? 0} 只`;
  if (check.key === 'governance') return `事项 ${e.total ?? 0}；实验或后续阶段 ${e.experiment_or_later ?? 0}；阻断 ${e.blocked ?? 0}；超时未进实验 ${e.stale_pre_experiment ?? 0}`;
  if (check.key === 'tracking') return `已评价 ${e.evaluated ?? 0} 条；行情或复权缺口 ${e.gaps ?? 0} 条`;
  if (check.key === 'delivery') return `已发送 ${e.sent ?? 0}；失败 ${e.failed ?? 0}`;
  if (check.key === 'scan') return `${e.as_of_date ?? '未知日期'} · ${e.lane_count ?? 0} 套策略`;
  if (check.key === 'news') return `研究截至 ${e.cutoff ?? '未知'}；距核查 ${Number(e.age_hours ?? 0).toFixed(1)} 小时`;
  if (check.key === 'indices' && Array.isArray(e)) return `取得 ${e.length}/7 个指数日线`;
  return '';
}
</script>

<template>
  <section class="business-coverage" aria-label="业务覆盖验收">
    <header><div><h2>业务覆盖验收</h2><p>分项核对真实数据，不把接口在线、测试通过当作整套业务正常。</p></div>
      <button :disabled="loading" @click="load">{{ loading ? '正在核查…' : report ? '重新核查' : '展开只读核查' }}</button></header>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p class="overall" :class="{ attention: report.status !== 'passed' }">{{ report.status === 'passed' ? '本次分项检查通过；不代表未来可靠性或收益保证。' : '存在未通过项，不能认定整套系统已可用。' }}</p>
      <p>核查时点：{{ new Date(report.checked_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }}（北京时间）</p>
      <div class="checks"><article v-for="check in report.checks" :key="check.key">
        <h3>{{ check.label }} <span :class="{ attention: check.status !== 'passed' }">{{ check.status === 'passed' ? '本项通过' : '需要处理' }}</span></h3>
        <p>{{ evidence(check) }}</p><small>{{ check.reason }}</small>
      </article></div>
    </template>
  </section>
</template>

<style scoped>
.business-coverage{border:1px solid #dce3ed;border-radius:12px;background:var(--el-bg-color,#fff);padding:20px;margin-bottom:20px;min-width:0}
header{display:flex;justify-content:space-between;align-items:center;gap:16px}h2{font-size:18px;margin:0}h3{font-size:14px;margin:0}p,small{font-size:13px;line-height:1.7;overflow-wrap:anywhere}header p,small{color:var(--el-text-color-secondary,#66758b)}
button{border:1px solid #347de0;color:#347de0;background:transparent;border-radius:6px;padding:9px 13px;cursor:pointer;flex-shrink:0}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:12px}.checks article{border:1px solid #dce3ed;padding:14px;border-radius:8px;min-width:0}h3 span{float:right;font-size:12px;color:#2b7354}.attention{color:#a15c10!important}.overall{font-weight:600}@media(max-width:640px){header{align-items:flex-start;flex-direction:column}.business-coverage{padding:14px}}
</style>

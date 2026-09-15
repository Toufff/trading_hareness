<script setup lang="ts">
import { priceVolumeLabel, priceVolumeValue, type PriceVolumeEvidence } from './price-volume-evidence';
defineProps<{ evidence?: PriceVolumeEvidence }>();
const statusText: Record<string, string> = { verified_daily:'日线检查已完成', quality_warning:'量价质量有警示', quality_unverified:'量价质量尚不能确认' };
</script>

<template>
  <section class="pv-evidence" aria-label="量价证据与边界">
    <h5>量价证据 <span>{{ evidence ? (statusText[evidence.status] ?? '证据状态未知') : '本轮未附量价证据' }}</span></h5>
    <p v-if="!evidence">未提供不代表检查通过；不能据此认定已缩量承接、有效突破或成功回封。</p>
    <template v-else>
      <p class="scope">{{ evidence.scope }} 日线检查不是盘中买入触发，也不代表盈利已验证。</p>
      <div class="pv-columns">
        <div><h6>已计算的日线事实</h6>
          <p v-if="!evidence.reasons?.length">暂无已确认的量价结论；请看原始指标与缺口。</p>
          <ul v-else><li v-for="reason in evidence.reasons" :key="reason">{{ reason }}</li></ul>
          <dl><div v-for="(value, key) in evidence.metrics" :key="key"><dt>{{ priceVolumeLabel(key) }}</dt><dd>{{ priceVolumeValue(key, value) }}</dd></div></dl>
        </div>
        <div><h6>风险与尚未验证</h6>
          <ul v-if="evidence.warnings?.length" class="warnings"><li v-for="warning in evidence.warnings" :key="warning">{{ warning }}</li></ul>
          <p v-else>本证据对象未给出额外警示，不等于没有风险。</p>
          <ul><li v-for="unknown in evidence.unverified" :key="unknown">尚未验证：{{ priceVolumeLabel(unknown) }}</li></ul>
          <h6>同源结构参考位</h6>
          <dl><div v-for="(value, key) in evidence.references" :key="key"><dt>{{ priceVolumeLabel(key) }}</dt><dd>{{ value == null ? '缺少数据' : `${value.toFixed(2)} 元` }}</dd></div></dl>
        </div>
      </div>
      <p class="version">证据版本：{{ evidence.version }}。下方确认条件是未来等待触发的条件，不是已经发生的事实。</p>
    </template>
  </section>
</template>

<style scoped>
.pv-evidence{background:var(--el-fill-color-light,#f5f7fa);border:1px solid var(--el-border-color-lighter,#e4eaf0);padding:14px;border-radius:8px;margin:14px 0;font-size:12px;line-height:1.7;min-width:0}.pv-evidence h5{font-size:14px;margin:0 0 8px}.pv-evidence h5 span{font-size:12px;font-weight:normal;margin-left:12px}.pv-evidence h6{font-size:12px;margin:10px 0 4px}.pv-evidence p{font-size:12px;margin:6px 0}.pv-columns{display:grid;grid-template-columns:1fr 1fr;gap:20px;min-width:0}.pv-columns>div{min-width:0}.pv-evidence ul{padding-left:18px;margin:6px 0}.pv-evidence dl{margin:8px 0}.pv-evidence dl>div{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--el-border-color-lighter,#e4eaf0);padding:4px 0}.pv-evidence dt{overflow-wrap:anywhere}.pv-evidence dd{margin:0;flex-shrink:0}.warnings{color:var(--el-color-warning-dark-2,#966219)}.scope,.version{color:var(--el-text-color-secondary,#66758b);overflow-wrap:anywhere}@media(max-width:640px){.pv-columns{grid-template-columns:1fr;gap:4px}.pv-evidence{padding:10px}.pv-evidence h5 span{display:block;margin:4px 0}}
</style>

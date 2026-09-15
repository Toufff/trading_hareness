<script setup lang="ts">
import type { StrategyLane } from './short-term-reports';
import PriceVolumeEvidence from './PriceVolumeEvidence.vue';
defineProps<{ lane: StrategyLane }>();
</script>

<template>
  <section class="lane-detail" :data-strategy-detail="lane.key">
    <section v-if="lane.observation_list?.length" data-testid="independent-discovery">
      <h3>独立结构观察 · 不等于可买</h3>
      <p class="muted">{{ lane.observation_policy }}</p>
      <article v-for="pick in lane.observation_list" :key="pick.symbol">
        <h4>{{ pick.name }}（{{ pick.symbol.split('.')[0] }}）<small>{{ pick.sector_label }}</small></h4>
        <p><strong>观察理由：</strong>{{ pick.reason }}</p>
        <p><strong>风险与执行：</strong>{{ pick.caution }}</p>
        <p><strong>等待确认：</strong>{{ pick.confirmation }}</p>
        <p><strong>放弃条件：</strong>{{ pick.invalidation }}</p>
        <p v-if="pick.attention" class="muted">交易基础分 {{ pick.liquidity?.score.toFixed(0) }} · 成交参与度 {{ pick.attention.participation_percentile.toFixed(0) }} 分位（本策略 {{ pick.attention.sample_count }} 个样本） · 成交额/前5日均额 {{ pick.attention.amount_multiple?.toFixed(2) ?? '未知' }} 倍。热度单列，暂不改变正式排名。</p>
      </article>
    </section>
    <h3>候选明细与观察条件</h3>
    <p class="muted">匹配 {{ lane.total_matches }} 只，展示 {{ lane.selected.length }} 只；这里不是全量匹配名单。</p>
    <aside v-if="lane.factor_policy?.enabled" class="factor-note" data-testid="factor-policy">
      <strong>已启用可选排序因子</strong> · {{ lane.factor_policy.rule }}
      <p v-for="factor in lane.factor_policy.factors" :key="factor.key">
        {{ factor.key === 'small_cap' ? '小盘偏好' : factor.key }} · 加分预算 {{ (factor.weight * 100).toFixed(0) }} 分 · 市值覆盖
        {{ factor.context.valid_count }}/{{ factor.context.universe_count }} · {{ factor.context.cap_basis }}
        <span v-if="factor.context.status !== 'ready'"> · 数据不足，本轮未加分</span>
      </p>
    </aside>
    <p v-if="!lane.selected.length">{{ lane.empty_reason }}</p>
    <article v-for="pick in lane.selected" :key="pick.symbol">
      <h4>{{ pick.name }}<span>（{{ pick.symbol.split('.')[0] }}）</span><small>{{ pick.sector_label }}</small></h4>
      <p><strong>为什么观察：</strong>{{ pick.reason }}</p>
      <div v-if="pick.factor_overlay" class="factor-note" data-testid="factor-explanation">
        <p>{{ pick.factor_overlay.explanation }}</p>
        <p class="muted">{{ pick.factor_overlay.rank_scope }}</p>
        <p v-for="factor in pick.factor_overlay.factors" :key="factor.key">{{ factor.explanation }}；加 {{ factor.bonus.toFixed(2) }} 分。</p>
      </div>
      <p class="muted">收盘 {{ pick.metrics.close.toFixed(2) }} · {{ pick.metrics.change_pct.toFixed(2) }}% · 成交 {{ (pick.metrics.amount / 1e8).toFixed(2) }} 亿 · 换手 {{ pick.metrics.turnover.toFixed(2) }}%</p>
      <PriceVolumeEvidence :evidence="pick.price_volume" />
      <p><strong>等待触发的确认条件：</strong>{{ pick.confirmation }}</p>
      <p><strong>放弃条件：</strong>{{ pick.invalidation }}</p>
      <p><strong>当前注意：</strong>{{ pick.caution }}</p>
      <p class="muted">有效期：{{ pick.expiry }}。收盘结构参考，不是分钟止损线。</p>
    </article>
    <details v-if="lane.caution_list?.length">
      <summary>过热或转弱观察（{{ lane.caution_list.length }}）</summary>
      <div v-for="pick in lane.caution_list" :key="pick.symbol"><p>{{ pick.name }}（{{ pick.symbol.split('.')[0] }}）：{{ pick.reason }}；{{ pick.caution }}</p><PriceVolumeEvidence :evidence="pick.price_volume" /></div>
    </details>
  </section>
</template>

<style scoped>
.factor-note{padding:10px 14px;background:var(--el-fill-color-light);border-left:3px solid var(--el-color-primary);font-size:12px;line-height:1.7;margin:12px 0}
.lane-detail{max-width:1100px}.lane-detail article{border-top:1px solid var(--el-border-color-lighter);padding:18px 0}.lane-detail h3{font-size:16px}.lane-detail h4{font-size:15px;margin:0 0 8px}.lane-detail h4 span{font-size:12px;font-weight:normal}.lane-detail small{margin-left:12px;color:var(--el-text-color-secondary);font-weight:normal}.lane-detail p{font-size:13px;line-height:1.8;margin:8px 0}.lane-detail .muted{font-size:12px;color:var(--el-text-color-secondary)}summary{cursor:pointer;color:var(--el-color-primary);font-size:13px}
</style>

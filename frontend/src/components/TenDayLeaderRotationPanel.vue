<script setup lang="ts">
import type { components } from '../api/generated';

type RotationRun = components['schemas']['TenDayLeaderRotationRunResponse'];
type RotationCandidate = components['schemas']['TenDayLeaderRotationCandidateResponse'];
type IntradaySnapshot = components['schemas']['TenDayLeaderRotationIntradayResponse'];
type RotationResponse = Omit<Partial<components['schemas']['TenDayLeaderRotationLatestResponse']>, 'scope'> & { scope?: string };

defineProps<{
  data?: RotationResponse;
  loading?: boolean;
}>();
defineEmits<{ run: [] }>();

function statusType(status?: string | null) {
  return status === 'completed' ? 'success' : status === 'blocked' ? 'warning' : status === 'partial' ? 'info' : 'info';
}

function dateText(value?: string | null) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}
</script>

<template>
  <el-card shadow="never" class="section-gap">
    <template #header>
      <div class="card-header">
        <span>十日排行榜龙头协同（影子研究）</span>
        <el-space>
          <el-tag :type="statusType(data?.run?.status)">{{ data?.run?.status ?? '未部署/未运行' }}</el-tag>
          <el-button type="primary" :loading="loading" @click="$emit('run')">重算影子池</el-button>
        </el-space>
      </div>
    </template>
    <el-alert title="仅使用已落库的点时日线与后续盘中证据；它不生成订单，也不会绕过历史完整性门禁。" type="info" :closable="false" show-icon/>
    <el-alert v-if="data?.run?.status === 'blocked'" class="section-gap" :title="String(data.run.summary?.reason ?? '当前历史证据不足，未生成候选。')" type="warning" :closable="false" show-icon/>
    <el-alert v-else-if="!data?.run && data?.notice" class="section-gap" :title="data.notice" type="info" :closable="false" show-icon/>
    <el-descriptions v-if="data?.run" :column="3" border size="small" class="section-gap">
      <el-descriptions-item label="交易日">{{ data.run.as_of_date ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="模型版本">{{ data.run.model_version ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="最近更新">{{ dateText(data.run.updated_at) }}</el-descriptions-item>
      <el-descriptions-item label="日线覆盖">{{ data.run.source_status?.daily_symbols ?? '-' }} / {{ data.run.source_status?.minimum_full_market_symbols ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="完整十日样本">{{ data.run.source_status?.eligible_symbols ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="历史未完整">{{ data.run.source_status?.incomplete_history_symbols ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="候选数">{{ data.run.summary?.candidate_count ?? data.candidates?.length ?? 0 }}</el-descriptions-item>
    </el-descriptions>
    <el-descriptions v-if="data?.intraday?.latest_batch" :column="3" border size="small" class="section-gap">
      <el-descriptions-item label="盘中候选池日期">{{ data.intraday.pool_run?.as_of_date ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="最近盘中观测">{{ dateText(data.intraday.latest_batch.observed_at) }}</el-descriptions-item>
      <el-descriptions-item label="本轮观测 / 影子合格">{{ data.intraday.latest_batch.observed_count ?? 0 }} / {{ data.intraday.latest_batch.shadow_eligible_count ?? 0 }}</el-descriptions-item>
      <el-descriptions-item label="报价来源">{{ data.intraday.latest_batch.quote_sources?.join('、') || '-' }}</el-descriptions-item>
      <el-descriptions-item label="可下单数">{{ data.intraday.latest_batch.decision_eligible_count ?? 0 }}（固定为 0）</el-descriptions-item>
      <el-descriptions-item label="盘中模型">{{ data.intraday.pool_run?.model_version ?? '-' }}</el-descriptions-item>
    </el-descriptions>
    <el-empty v-if="data?.run && !(data.candidates?.length)" description="没有可展示候选；请先满足完整十日历史门禁" :image-size="54" class="section-gap"/>
    <el-table v-else-if="data?.candidates?.length" :data="data.candidates" size="small" max-height="300" class="section-gap">
      <el-table-column prop="board_rank" label="#" width="48"/>
      <el-table-column prop="symbol" label="代码" width="106"/>
      <el-table-column prop="name" label="名称" min-width="98"/>
      <el-table-column prop="board" label="板块" width="82"/>
      <el-table-column label="十日涨幅" width="94"><template #default="{ row }">{{ row.ten_day_return_pct ?? '-' }}%</template></el-table-column>
      <el-table-column label="影子状态" min-width="145"><template #default="{ row }"><el-space wrap><el-tag size="small" :type="row.shadow_eligible ? 'success' : 'info'">{{ row.shadow_state }}</el-tag><el-tag v-for="reason in row.reason_codes ?? []" :key="reason" size="small" type="warning">{{ reason }}</el-tag></el-space></template></el-table-column>
    </el-table>
  </el-card>
</template>

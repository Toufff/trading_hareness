<script lang="ts">
import { defineComponent, inject } from 'vue';
import { Refresh, WarningFilled } from '@element-plus/icons-vue';
import VChart from 'vue-echarts';
import { dashboardContextKey } from '../../dashboard-context';

export default defineComponent({
  name: 'ResearchOverviewTab',
  components: { Refresh, VChart, WarningFilled },
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('research tab requires the dashboard shell context');
    return dashboard as Record<string, any>;
  },
});
</script>

<template>

  <el-row :gutter="14" class="metric-row"><el-col v-for="metric in [{label:'远端报告',value:count('remote_reports')},{label:'结构化观点',value:count('claims')},{label:'标准日线',value:count('canonical_bars')},{label:'质量问题',value:count('quality_issues')}]" :key="metric.label" :xs="12" :md="6"><el-card shadow="never" class="metric-card"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong></el-card></el-col></el-row>
  <el-card shadow="never" class="section-gap" header="盘后一键更新">
    <el-alert title="按依赖顺序刷新全A基准与日线、腾讯收盘快照、AKShare/东财补充、同花顺资金流、巨潮公告、龙虎榜背景、板块复盘、分析师结算和盘后策略。某源尚未发布时其余步骤仍会完成，并会显示待重试项。" type="info" :closable="false" show-icon/>
    <div class="card-actions">
      <el-button type="primary" :icon="Refresh" :loading="actionLoading === '盘后一键更新'" @click="runPostCloseRefresh">盘后一键更新</el-button>
      <el-tag v-if="postCloseRefresh" :type="postCloseRefresh.status === 'completed' ? 'success' : 'warning'">{{ postCloseRefresh.status }}</el-tag>
      <el-text v-if="postCloseRefresh?.trade_date" type="info">交易日 {{ postCloseRefresh.trade_date }} · 日线{{ postCloseRefresh.daily_ready ? '已就绪' : '待发布/待重试' }} · 控制面{{ postCloseRefresh.controls_ready ? '已就绪' : '待补全/策略已阻断' }}</el-text>
      <el-text v-if="postCloseRefresh?.deferred_stages?.length" type="warning">待处理：{{ postCloseRefresh.deferred_stages.join('、') }}</el-text>
    </div>
    <el-text v-if="postCloseRefresh?.retry_hint" type="warning">{{ postCloseRefresh.retry_hint }}</el-text>
  </el-card>
  <el-row :gutter="14">
    <el-col :md="8" :xs="24">
      <el-card shadow="never" header="历史数据容量评估">
        <el-statistic title="3年P0/P1日频估算" :value="Number(overview.history_estimate?.estimated_storage_gib ?? 0)" suffix="GiB" :precision="2"/>
        <el-text type="info">不含分钟线；历史分钟仍只走离线文件。</el-text>
        <el-table :data="historyDatasetRows" size="small" max-height="238" class="section-gap">
          <el-table-column prop="label" label="数据集" min-width="120" show-overflow-tooltip/>
          <el-table-column label="行数" width="95"><template #default="{ row }">{{ rowText(row.rows) }}</template></el-table-column>
          <el-table-column label="存储" width="82"><template #default="{ row }">{{ storageText(row.estimated_storage_gib) }}</template></el-table-column>
        </el-table>
      </el-card>
    </el-col>
    <el-col :md="8" :xs="24">
      <el-card shadow="never" header="当前覆盖">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="日线区间">{{ displayValue(overview.data_coverage?.first_bar_date) }} - {{ displayValue(overview.data_coverage?.latest_bar_date) }}</el-descriptions-item>
          <el-descriptions-item label="有效交易日">{{ rowText(overview.data_coverage?.bar_days) }}</el-descriptions-item>
          <el-descriptions-item label="全截面天数">{{ rowText(overview.data_coverage?.full_cross_section_days) }}</el-descriptions-item>
          <el-descriptions-item label="最大单日股票数">{{ rowText(overview.data_coverage?.max_symbols_on_day) }}</el-descriptions-item>
          <el-descriptions-item label="分钟覆盖股票">{{ rowText(overview.data_coverage?.minute_symbols) }}</el-descriptions-item>
        </el-descriptions>
      </el-card>
    </el-col>
    <el-col :md="8" :xs="24">
      <el-card shadow="never" header="运行健康">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="运行中任务">{{ count('running_fetch_runs') }}</el-descriptions-item>
          <el-descriptions-item label="陈旧任务"><el-tag :type="readinessType(count('stale_fetch_runs'))">{{ count('stale_fetch_runs') }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="全市场股票">{{ rowText(count('all_a_symbols')) }}</el-descriptions-item>
          <el-descriptions-item label="板块成分">{{ rowText(count('active_sector_memberships')) }}</el-descriptions-item>
        </el-descriptions>
        <div class="card-actions"><el-button :disabled="!count('stale_fetch_runs')" :loading="actionLoading === '修复陈旧运行任务'" @click="reconcileStaleFetchRuns">修复陈旧任务</el-button></div>
      </el-card>
    </el-col>
  </el-row>
  <el-row :gutter="14"><el-col :md="14" :xs="24"><el-card shadow="never" header="数据快照"><template v-if="overview.latest_snapshot"><el-descriptions :column="1" border><el-descriptions-item label="状态"><el-tag :type="overview.latest_snapshot.status === 'ready' ? 'success' : 'warning'">{{ overview.latest_snapshot.status }}</el-tag></el-descriptions-item><el-descriptions-item label="截至日期">{{ overview.latest_snapshot.as_of_date }}</el-descriptions-item><el-descriptions-item label="知识截止">{{ dateText(overview.latest_snapshot.knowledge_cutoff) }}</el-descriptions-item></el-descriptions></template><el-empty v-else description="尚无研究快照" :image-size="72" /><div class="card-actions"><el-button :loading="actionLoading === '构建快照'" @click="runAction('构建快照','/api/research/snapshots/build')">构建快照</el-button><el-button type="primary" :loading="actionLoading === '运行日常管线'" @click="runAction('运行日常管线','/api/research/pipeline/daily')">运行日常管线</el-button></div></el-card></el-col><el-col :md="10" :xs="24"><el-card shadow="never" header="最新候选池"><el-empty v-if="!recommendations.length" description="没有可展示候选" :image-size="72" /><el-table v-else :data="recommendations.slice(0, 5)" size="small"><el-table-column prop="rank" label="#" width="48"/><el-table-column prop="symbol" label="标的"/><el-table-column prop="score" label="评分" width="70"/><el-table-column prop="decision" label="结论"/></el-table></el-card></el-col></el-row>
  <el-card shadow="never" header="研究运行"><el-space wrap><el-button :loading="actionLoading === '重算远端报告观点'" @click="runAction('重算远端报告观点','/api/research/reports/reprocess',{ limit: 100 }, true)">重算远端报告观点</el-button><el-button :loading="actionLoading === '重算观点结果'" @click="runAction('重算观点结果','/api/research/outcomes/recompute')">重算观点结果</el-button><el-button :loading="actionLoading === '重算分析师评分卡'" @click="runAction('重算分析师评分卡','/api/research/scorecards/recompute')">重算分析师评分卡</el-button><el-tag type="info">原始记录 {{ count('tushare_raw_records') }}</el-tag><el-tag type="info">离线分钟 {{ count('offline_minute_bars') }}</el-tag></el-space></el-card>
  <el-card shadow="never" header="运行与历史验证 Readiness">
    <el-alert :title="overview.feature_readiness?.decision_ready ? '运行基线：核心决策数据已通过当前门槛' : `运行基线阻塞：${(overview.feature_readiness?.blockers ?? []).join(', ') || '未知'}`" :type="overview.feature_readiness?.decision_ready ? 'success' : 'warning'" :closable="false" show-icon/>
    <el-alert class="section-gap" :title="replayReadiness.status === 'ready' ? '历史验证：P2 数据基础与 P3 回放门槛均已满足' : '历史验证：尚未达到回放/策略验证门槛；不会被运行基线的绿色状态掩盖。'" :type="replayReadiness.status === 'ready' ? 'success' : 'warning'" :closable="false" show-icon/>
    <el-descriptions :column="mobileLayout ? 1 : 4" border size="small" class="section-gap">
      <el-descriptions-item label="P2 数据基础"><el-tag :type="replayReadiness.p2_data_foundation_ready ? 'success' : 'warning'">{{ replayReadiness.p2_data_foundation_ready ? '已满足' : '待补齐' }}</el-tag></el-descriptions-item>
      <el-descriptions-item label="P3 策略验证"><el-tag :type="replayReadiness.p3_strategy_validation_ready ? 'success' : 'warning'">{{ replayReadiness.p3_strategy_validation_ready ? '已满足' : '待补齐' }}</el-tag></el-descriptions-item>
      <el-descriptions-item label="PIT 完整截面">{{ displayValue(replayReadiness.evidence?.full_cross_section_days) }} / 720</el-descriptions-item>
      <el-descriptions-item label="离线分钟回放天数">{{ displayValue(replayReadiness.evidence?.offline_minute_trading_days) }} / 60</el-descriptions-item>
    </el-descriptions>
    <el-text type="info">历史口径：按当日 All-A 历史成员、日线、daily_basic 与涨跌停控制面共同判定；前向捕获不替代历史回放。</el-text>
    <el-table :data="featureReadinessRows" size="small" max-height="330" class="section-gap">
      <el-table-column prop="feature" label="特征" min-width="130"/>
      <el-table-column prop="priority" label="优先级" width="82"/>
      <el-table-column label="状态" width="92"><template #default="{ row }"><el-tag :type="featureStatusType(row.status)">{{ row.status }}</el-tag></template></el-table-column>
      <el-table-column label="覆盖对象" width="105"><template #default="{ row }">{{ rowText(row.symbols) }}</template></el-table-column>
      <el-table-column label="记录数" width="110"><template #default="{ row }">{{ rowText(row.rows) }}</template></el-table-column>
      <el-table-column label="覆盖率" width="92"><template #default="{ row }">{{ row.coverage === null || row.coverage === undefined ? '-' : `${Math.round(row.coverage * 10000) / 100}%` }}</template></el-table-column>
      <el-table-column label="最新日期" width="120"><template #default="{ row }">{{ displayValue(row.latest_date) }}</template></el-table-column>
    </el-table>
  </el-card>

</template>

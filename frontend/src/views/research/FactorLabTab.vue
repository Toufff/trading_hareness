<script lang="ts">
import { defineComponent, inject } from 'vue';
import VChart from 'vue-echarts';
import { dashboardContextKey } from '../../dashboard-context';

export default defineComponent({
  name: 'FactorLabTab',
  components: { VChart },
  setup() {
    const dashboard = inject(dashboardContextKey);
    if (!dashboard) throw new Error('research tab requires the dashboard shell context');
    return dashboard;
  },
});
</script>

<template>

  <el-alert title="全A点时股票池 + 后复权研究价；当前只有一年完整日线（242/732 日），结果只能用于探索，不进入盘中策略。行业分类仍是当前口径，正式晋级前还要补点时行业历史。" type="warning" :closable="false" show-icon />
  <el-card shadow="never"><template #header><div class="card-header"><span>因子实验控制台</span><el-space><el-select v-model="factorHorizon" class="factor-horizon"><el-option :value="1" label="1日收益"/><el-option :value="5" label="5日收益"/><el-option :value="20" label="20日收益"/></el-select><el-button type="primary" :disabled="!selectedFactors.length" :loading="actionLoading === '评估因子'" @click="runFactorEvaluation">评估所选因子</el-button></el-space></div></template><el-checkbox-group v-model="selectedFactors" class="factor-picker"><el-checkbox v-for="factor in factors" :key="factor.factor_key" :value="factor.factor_key" :disabled="factor.implementation !== 'native_sql'">{{ factor.label }} <el-tag size="small" :type="factor.implementation === 'native_sql' ? 'success' : 'info'">{{ factor.implementation }}</el-tag></el-checkbox></el-checkbox-group></el-card>
  <el-row :gutter="14"><el-col :md="13" :xs="24"><el-card shadow="never" header="Rank IC 对比"><VChart class="research-chart" :option="factorChartOption" autoresize/><el-empty v-if="!factorEvaluations.length" description="尚未运行因子评估" :image-size="68"/></el-card></el-col><el-col :md="11" :xs="24"><el-card shadow="never" header="因子注册表"><el-table :data="factors" max-height="330" size="small"><el-table-column prop="label" label="因子"/><el-table-column prop="category" label="类别" width="95"/><el-table-column prop="version" label="版本" width="90"/><el-table-column label="框架" min-width="145"><template #default="{ row }"><el-space wrap><el-tag v-for="tag in row.framework_tags" :key="tag" size="small" type="info">{{ tag }}</el-tag></el-space></template></el-table-column></el-table></el-card></el-col></el-row>
  <el-card shadow="never" header="最新因子评估结果"><el-table :data="latestFactorEvaluations" max-height="380"><el-table-column prop="label" label="因子" min-width="120"/><el-table-column label="研究门禁" width="120"><template #default="{ row }"><el-tag type="warning">{{ nestedValue(row.metrics,'promotion_gate.status') ?? row.status }}</el-tag></template></el-table-column><el-table-column prop="horizon_days" label="周期" width="65"/><el-table-column prop="observations" label="样本" width="105"/><el-table-column prop="cross_section_days" label="截面日" width="80"/><el-table-column label="全样本中性IC" width="115"><template #default="{ row }">{{ nestedNumber(row.metrics,'neutral_rank_ic.mean') }}</template></el-table-column><el-table-column label="样本外IC" width="100"><template #default="{ row }">{{ nestedNumber(row.metrics,'walk_forward.test.neutral_rank_ic.mean') }}</template></el-table-column><el-table-column label="BH q值" width="95"><template #default="{ row }">{{ nestedNumber(row.metrics,'multiple_testing.test_q_value') }}</template></el-table-column><el-table-column label="中性多空差" width="110"><template #default="{ row }">{{ nestedNumber(row.metrics,'neutral_top_minus_bottom.mean') }}</template></el-table-column><el-table-column label="顶部换手" width="100"><template #default="{ row }">{{ metricNumber(row.metrics,'top_bucket_turnover') }}</template></el-table-column></el-table></el-card>
  <el-card shadow="never" class="section-gap">
    <template #header><div class="card-header"><div><span>观察池主升启动 · v2 可空仓影子 Challenger</span><small class="realtime-refresh-time">一年日线；T 收盘形成形态先验，T+1 开盘入场标签；旧测试窗仅作诊断，必须由未来新窗口验证。</small></div><el-button type="primary" :loading="actionLoading === '训练观察池主升影子模型'" @click="runMainWaveResearch">重新训练</el-button></div></template>
    <el-alert :title="latestMainWaveExperiment ? `当前门禁：${nestedValue(latestMainWaveExperiment.metrics,'promotion_gate.status')}。未晋级时只写实时影子证据，不发飞书、不进入决策分。` : '尚未训练观察池主升模型。'" :type="nestedValue(latestMainWaveExperiment?.metrics,'promotion_gate.status') === 'eligible_for_manual_review' ? 'success' : 'warning'" :closable="false" show-icon/>
    <template v-if="latestMainWaveExperiment">
      <el-row :gutter="12" class="metric-row section-gap">
        <el-col :md="4" :xs="12"><el-statistic title="总样本" :value="Number(nestedValue(latestMainWaveExperiment.metrics,'sample_rows') || 0)"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="形态排序AUC" :value="Number(nestedValue(latestMainWaveExperiment.metrics,'walk_forward.test.roc_auc') || 0)" :precision="3"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="形态命中" :value="Number(nestedValue(latestMainWaveExperiment.metrics,'walk_forward.test.selected_precision') || 0) * 100" suffix="%" :precision="1"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="相对基准Lift" :value="Number(nestedValue(latestMainWaveExperiment.metrics,'walk_forward.test.selected_lift') || 0)" :precision="2"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="形态后10日" :value="Number(nestedValue(latestMainWaveExperiment.metrics,'walk_forward.test.selected_terminal_return') || 0) * 100" suffix="%" :precision="2"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="空仓天数" :value="Number(nestedValue(latestMainWaveExperiment.metrics,'walk_forward.test.abstained_dates') || 0)"/></el-col>
      </el-row>
      <el-alert class="section-gap" type="info" :closable="false" show-icon :title="`v1 诊断：测试正样本率仅为训练期的 ${(Number(nestedValue(latestMainWaveExperiment.metrics,'failure_diagnosis.base_rate_shift.test_to_train_ratio') || 0) * 100).toFixed(1)}%，有 ${Number(nestedValue(latestMainWaveExperiment.metrics,'failure_diagnosis.feature_direction_flip_count') || 0)} 个特征方向翻转；静态概率已失准。`"/>
      <el-divider content-position="left">v2 形态资格条件</el-divider>
      <el-space wrap><el-tag v-for="item in mainWaveQualification" :key="item.key" type="success">{{ item.key }} = {{ item.threshold }}</el-tag></el-space>
      <el-divider content-position="left">尚未通过的研究门禁</el-divider>
      <el-space wrap><el-tag v-for="key in mainWaveFailedChecks" :key="key" type="warning">{{ key }}</el-tag></el-space>
      <el-divider content-position="left">下一交易日影子先验</el-divider>
      <el-table :data="mainWaveCurrentScores.slice(0, 12)" max-height="330" size="small">
        <el-table-column prop="rank" label="#" width="46"/><el-table-column prop="symbol" label="代码" width="105"/><el-table-column prop="name" label="名称" min-width="95"/>
        <el-table-column label="形态强度" width="95"><template #default="{ row }">{{ (Number(row.model_score) * 100).toFixed(1) }}%</template></el-table-column>
        <el-table-column label="池内分位" width="90"><template #default="{ row }">{{ (Number(row.percentile) * 100).toFixed(1) }}%</template></el-table-column>
        <el-table-column label="影子状态" min-width="130"><template #default="{ row }"><el-tag size="small" :type="row.state === 'shadow_confirmed' ? 'warning' : row.state === 'shadow_forming' ? 'success' : 'info'">{{ row.state === 'shadow_confirmed' ? '形态确认待盘中' : row.state === 'shadow_forming' ? '蓄势观察' : '普通观察' }}</el-tag></template></el-table-column>
      </el-table>
    </template>
  </el-card>
  <el-card shadow="never" class="section-gap">
    <template #header><div class="card-header"><div><span>科技下跌浪 · 恐慌耗竭与B浪反弹</span><small class="realtime-refresh-time">恐慌只观察；单日普涨只算试探；收盘确认后仍需盘中量价承接，才给观察池发研究提醒。</small></div><el-tag type="warning">research-alert</el-tag></div></template>
    <el-alert v-if="!latestReboundExperiment" title="尚未生成逆势反弹研究结果，点击上方重新训练会同时运行。" type="info" :closable="false" show-icon/>
    <template v-else>
      <el-alert :title="`当前门禁：${nestedValue(latestReboundExperiment.metrics,'promotion_gate.status')}。恐慌与试探阶段不提醒；确认后只对显式观察池发送带原因、失效条件和低置信度研究概率的人工复核卡。`" type="warning" :closable="false" show-icon/>
      <el-row :gutter="12" class="metric-row section-gap">
        <el-col :md="4" :xs="12"><el-statistic title="科技样本" :value="Number(nestedValue(latestReboundExperiment.metrics,'sample_rows') || 0)"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="测试确认日" :value="Number(nestedValue(latestReboundExperiment.metrics,'walk_forward.test.selected_dates') || 0)"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="确认命中" :value="Number(nestedValue(latestReboundExperiment.metrics,'walk_forward.test.selected_precision') || 0) * 100" suffix="%" :precision="1"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="相对基准Lift" :value="Number(nestedValue(latestReboundExperiment.metrics,'walk_forward.test.selected_lift') || 0)" :precision="2"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="确认后5日净值" :value="Number(nestedValue(latestReboundExperiment.metrics,'walk_forward.test.selected_net_terminal_return') || 0) * 100" suffix="%" :precision="2"/></el-col>
        <el-col :md="4" :xs="12"><el-statistic title="恐慌期MFE" :value="Number(nestedValue(latestReboundExperiment.metrics,'walk_forward.test.panic_mfe') || 0) * 100" suffix="%" :precision="2"/></el-col>
      </el-row>
      <el-divider content-position="left">尚未通过的研究门禁</el-divider>
      <el-space wrap><el-tag v-for="key in reboundFailedChecks" :key="key" type="warning">{{ key }}</el-tag></el-space>
      <el-divider content-position="left">最新科技观察状态</el-divider>
      <el-table :data="reboundCurrentScores.slice(0, 19)" max-height="360" size="small">
        <el-table-column prop="rank" label="#" width="46"/><el-table-column prop="symbol" label="代码" width="105"/><el-table-column prop="name" label="名称" min-width="95"/>
        <el-table-column label="状态" min-width="145"><template #default="{ row }"><el-tag size="small" :type="row.state === 'shadow_confirmed' ? 'success' : row.state === 'shadow_panic' ? 'danger' : row.state === 'shadow_probe' ? 'warning' : 'info'">{{ row.pattern?.label ?? row.state }}</el-tag></template></el-table-column>
        <el-table-column label="强度" width="85"><template #default="{ row }">{{ (Number(row.model_score) * 100).toFixed(1) }}%</template></el-table-column>
        <el-table-column label="纪律" min-width="210"><template #default="{ row }">{{ row.pattern?.discipline ?? '-' }}</template></el-table-column>
      </el-table>
    </template>
  </el-card>
  <el-row :gutter="14"><el-col :md="8" :xs="24"><el-card shadow="never" header="A股约束回测"><el-form label-position="top"><el-form-item label="调仓间隔"><el-input-number v-model="backtestForm.rebalance_days" :min="1" :max="60"/></el-form-item><el-form-item label="持有天数"><el-input-number v-model="backtestForm.hold_days" :min="1" :max="60"/></el-form-item><el-form-item label="最多持仓"><el-input-number v-model="backtestForm.top_n" :min="1" :max="500"/></el-form-item><el-form-item label="单边成本 bps"><el-input-number v-model="backtestForm.total_cost_bps" :min="0" :max="500"/></el-form-item><el-button type="primary" :loading="actionLoading === '运行A股约束回测'" @click="runStrategyBacktest">运行回测</el-button></el-form></el-card></el-col><el-col :md="16" :xs="24"><el-card shadow="never" header="最新策略净值"><template v-if="latestExperiment"><el-row :gutter="12" class="metric-row"><el-col :span="6"><el-statistic title="总收益" :value="Number(latestExperiment.metrics.total_return || 0) * 100" suffix="%" :precision="2"/></el-col><el-col :span="6"><el-statistic title="年化收益" :value="Number(latestExperiment.metrics.annualized_return || 0) * 100" suffix="%" :precision="2"/></el-col><el-col :span="6"><el-statistic title="最大回撤" :value="Number(latestExperiment.metrics.max_drawdown || 0) * 100" suffix="%" :precision="2"/></el-col><el-col :span="6"><el-statistic title="交易笔数" :value="Number(latestExperiment.metrics.trades || 0)"/></el-col></el-row><VChart class="research-chart" :option="equityChartOption" autoresize/></template><el-empty v-else description="尚未运行策略实验" :image-size="68"/></el-card></el-col></el-row>
  <el-card shadow="never" header="开源框架与训练路线"><el-table :data="frameworks" max-height="280" size="small"><el-table-column prop="label" label="框架"/><el-table-column prop="role" label="用途"/><el-table-column prop="integration_mode" label="接入方式"/><el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.status === 'native' ? 'success' : row.status === 'planned' ? 'info' : 'warning'">{{ row.status }}</el-tag></template></el-table-column><el-table-column prop="license_note" label="许可" width="180"/></el-table><el-divider content-position="left">H100 训练门槛</el-divider><el-timeline><el-timeline-item v-for="stage in trainingRoadmap.stages" :key="stage.stage" :timestamp="stage.compute"><strong>{{ stage.stage }}</strong><div>{{ stage.gate }}</div></el-timeline-item></el-timeline><el-text type="info">{{ trainingRoadmap.policy }}</el-text></el-card>

</template>

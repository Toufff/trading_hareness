import { getJson } from '../api/http';

/** Read-only projection. No approval credential or mutation transport belongs here. */
export type GovernanceItem = {
  id?: string; item_id?: string; title?: string; status?: string; state?: string;
  revision?: number; artifact_hash?: string; artifact_sha256?: string;
  issue?: { problem?: string; hypothesis?: string; [key: string]: unknown };
  ready?: { summary?: string; risk?: string; rollback?: string; artifact_hash?: string; expires_at?: string; approval_revision?: number };
  current_experiment?: number | null;
  latest_diagnostic?: { status?: string; role?: string; reason?: string; system_owner?: string; next_step?: string; evidence_hash?: string; model_started?: boolean; recorded_at?: string } | null;
  experiments?: { spec: { change_kind?: string; validation_kind?: string; release_plan?: string; candidate_manifest?: string; [key: string]: unknown }; [key: string]: unknown }[];
  [key: string]: unknown;
};
export type GovernanceProjection = {
  status: string; items: GovernanceItem[]; counts?: Record<string, number>; notice?: string;
  truncated?: boolean; active?: { generation: number; item_id: string; artifact_hash: string; action: string; recorded_at: string } | null;
};
export const loadGovernance = (signal?: AbortSignal) =>
  getJson<GovernanceProjection>('/api/v1/strategy/governance', { signal });

const labels: Record<string, string> = {
  discovered:'待独立复核', proposed:'待独立设计', verified:'问题已复核', reviewed:'问题已复核',
  designed:'方案已冻结', design:'方案设计', experimenting:'实验中', experiment:'实验中',
  observing:'观察中', validated:'独立验收通过', ready:'待人工落地', ready_for_release:'待人工落地',
  ready_for_human:'待人工落地', ready_for_promotion:'待人工落地', approved:'人工已批准',
  active:'已人工启用', activated:'已人工启用', deployed:'已人工启用', rejected:'已驳回', blocked:'受阻',
  failed:'失败', revised:'已修订', rolled_back:'已回退', completed:'已完成',
  issue_id:'事项编号', hypothesis:'改进假设', problem:'问题', evidence:'证据', scope:'影响范围',
  baseline:'正式基线', candidate:'实验方案', criteria:'验收标准', acceptance:'验收标准',
  reviews:'独立复核意见', dissent:'不同意见', reason:'理由', decision:'结论',
  revisions:'修订记录', experiments:'实验记录', observations:'观察结果',
  events:'审计事件', actor:'执行身份', role:'职责', created_at:'记录时间', updated_at:'更新时间',
  artifact_hash:'工件哈希', artifact_sha256:'工件哈希', revision:'修订号', status:'状态',
  result:'结果', data_window:'数据窗口', limitations:'未验证范围', dependencies:'依赖事项',
  id:'事项编号', title:'标题', state:'阶段', issue:'原子问题', actors:'参与身份',
  observer:'发现者', reviewer:'独立复核者', designer:'方案设计者', implementer:'实施者', validator:'独立验收者',
  out_of_scope:'不修改范围', action:'动作', payload:'记录内容', spec:'冻结实验定义',
  artifact:'实验工件', comparison:'同数据对照', checks:'检查结果', sample_sufficient:'样本是否充分',
  independent_sessions:'独立交易日', validation:'验收意见', input_hash:'输入哈希',
  candidate_config:'实验配置', baseline_config:'基线配置', current_experiment:'当前实验序号',
  summary:'变更摘要', risk:'风险', rollback:'回退方案',
  expires_at:'审核有效期', approval_revision:'待批准修订', live_effect:'正式生效范围', none:'不影响正式策略', ranking_config:'排序配置',
  evaluator:'观察评价者',release_preparer:'发布准备者',input_manifest:'输入清单',
  minimum_sessions:'最低独立交易日数',minimum_observations:'最低观测数',
  evidence_hash:'证据哈希',profitability_claim:'是否宣称盈利有效',passed:'检查通过',insufficient:'样本不足',
  validation_kind:'验证种类',engineering:'工程行为验证',change_kind:'变更种类',code:'代码变更',
  allowed_strategy_keys:'允许影响的策略',baseline_config_hash:'基线配置哈希',baseline_code_hash:'基线代码哈希',candidate_code_hash:'候选代码哈希',
  latest_diagnostic:'最新执行诊断',system_owner:'系统负责角色',next_step:'下一步处理',model_started:'是否调用模型',recorded_at:'记录时间',
  proposer:'方案提出者',host_preflight:'运行环境预检',deferred:'本次暂缓',model_deferred:'模型执行暂缓',preflight_blocked:'环境预检受阻',
};
export function governanceLabel(value: string): string { return labels[value] ?? value; }
export function evidenceText(value: unknown): string {
  if (value == null) return '未提供';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value !== 'object') return governanceLabel(String(value));
  if (Array.isArray(value)) return value.map(evidenceText).join('；');
  return Object.entries(value).map(([key, entry]) => `${governanceLabel(key)}：${evidenceText(entry)}`).join('\n');
}

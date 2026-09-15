# 原子策略改进治理

2026-09-11。实现范围：问题、独立角色审核、冻结实验、证据比较、待人工落地队列及**排序因子配置**的人工启用/回退。工程通过不代表盈利有效。

## 边界

- 正式策略与治理记录隔离。发现、审核、实验、观察、验证、ready 均 `live_effect=none`。
- HTTP 仅 `GET /api/v1/strategy/governance`，无审批或修改接口；前端不能提交 actor 自称人类。
- 只有本机交互 CLI 能发起配置启用；没有 `--yes`。批准绑定问题 revision、冻结实验 SHA256、有效期、现用配置代次、配置 SHA256 和当前策略代码指纹。
- 新策略/代码改动可进入整个实验及 ready 流程，但本模块**不执行任意代码、不自动部署代码**，须走人工审核的正常不可变 release 流程。
- actor registry 是受信宿主上的角色分配，不是操作系统安全沙箱。拥有宿主/数据库完全权限的人仍可冒充身份、直接调用 Python 或改数据库。不同 ID 也不证明观点独立；必须审查原始证据。

## 组件

`app/strategy_governance/rules.py`：纯状态机/比较/授权检查。
`repository.py`：事务、乐观并发、追加审计、配置代次、工作租约。
`evidence.py`：实际读取测量文件并检查内容及 SHA256。
`configuration.py`：实际 strategy/ranking 模块代码指纹、现用环境配置快照。
`identity.py`：读取部署在仓库外的角色注册表。
`read_repository.py` 与 router：原生 async 只读投影。
迁移 `0092`：items 最新投影、events 不覆盖修订、activations 不覆盖代次、leases 有限期工作租约。

## 状态与角色

| 操作 | 前置→结果 | 角色与证据 |
|---|---|---|
| create | 无→discovered | observer；单一问题/假设/范围/不改范围/原证据/dedupe_key |
| review | discovered→reviewed | reviewer；不得为提出者；复现、反例、替代解释、确认结论 |
| design | reviewed→designed | designer；最小修改、代价、失败条件、回退、冻结 spec |
| experiment | designed→experiment | implementer；不得为该审核者；绑定 spec SHA256 与工件 |
| observe | experiment/observing→observing | evaluator；不得为实施者；测量文件必须实际存在且校验一致 |
| validate | observing→validated | validator；不得为此前任一角色；标准已通过、独立复现/泄漏/不利案例检查 |
| ready | validated→ready | release_preparer；风险、摘要、回退；7天有效窗口，不启用 |
| rework/reject | 非终态→discovered/rejected | 独立审核角色；原因必填；保留全部旧实验和审查记录 |
| activate | ready→activated | human；本机交互核准；只支持排序因子配置 |

依赖项须在实验/ready前 validated；正式配置启用前须 activated。循环依赖不会被猜测跳过，需人工重建合理依赖。每次转换要求精确 expected_revision；任何旧批准失效。原问题不就地改写；问题含义变化应新建关联原子事项。

## 冻结实验 spec

```json
{
  "change_kind": "ranking_config",
  "input_hash": "64位sha256",
  "baseline_code_hash": "当前实际策略模块指纹",
  "candidate_code_hash": "候选代码/工件指纹",
  "baseline_config_hash": "当前完整排序因子配置规范JSON的sha256",
  "baseline_generation": 0,
  "allowed_strategy_keys": ["accumulation"],
  "holdout_id": "预先登记且未调参的验收集编号",
  "data_start": "2026-09-01",
  "data_end": "2026-09-30",
  "minimum_sessions": 20,
  "minimum_observations": 50,
  "criteria": [{"metric":"declared_metric","operator":">=","threshold":0}],
  "candidate_config": {"ranking_factors":{"version":1,"factors":[]}}
}
```

以上20/50为格式演示，**不是所有策略的统一成熟门槛**。设计者应基于具体假设提前指定指标与样本门槛。
`allowed_strategy_keys` 为实际配置影响范围的硬限制：增加、删除、权重变化、`*` 扩散均按每个 lane 展开比对，不能声称只改一个策略却改变九个。
代码实验设 `change_kind=code`、`candidate_manifest`、`release_plan`，不允许混入 candidate_config。代码影响范围需独立检查 manifest/diff；此处不会声称任意代码作用域已被自动证明。

观测 evidence 必须同 input/code/holdout 哈希，包含去重后独立 sessions、observations、metrics、method、limitations、measurement_artifact、measurement_hash。
保存前检查 session 是否为已入库交易日，日期严格 ISO，测量文件 JSON 中的事实字段必须与提交字段一致。`passed:true` 无效，程序按冻结 criteria 重算是否过线；缺指标失败，缺样本 insufficient。
测量文件哈希证明可复现内容，不证明测量方法无偏；validator必须独立重跑，不能只接受作者的报告。持有本机权限的人可伪造输入，因此不宣称防恶意研究者。

重复 observe 保留旧观测历史；不能在看到结果后偷偷改变 criteria/数据范围。新数据若超出冻结范围或改变输入，需要新的实验修订并保留原试验，不能把开发集重新命名为 holdout。

## 本机 CLI 与角色登记

凭据和 actor registry 均放仓库外。进程环境 `STRATEGY_GOVERNANCE_ACTORS_FILE` 指向管理员配置的 JSON：

```json
{"actors": {
  "review-session-A":{"kind":"agent","roles":["reviewer"],"enabled":true},
  "owner":{"kind":"human","roles":["human"],"enabled":true}
}}
```

可创建更多独立会话角色；人类角色不允许混合 agent 权限。CLI 不接收任意角色列表，registry ID由本机受信配置解析；registry不是密码，不能防宿主管理员冒名。

```powershell
python scripts/strategy-governance.py create --actor observer-A --payload issue.json
python scripts/strategy-governance.py claim --actor reviewer-A --role reviewer
python scripts/strategy-governance.py get ISSUE_ID
python scripts/strategy-governance.py transition ISSUE_ID --actor reviewer-A --revision 1 --stage review --payload review.json
python scripts/strategy-governance.py activate ISSUE_ID --actor owner --revision 7 --artifact-hash SPEC_SHA256
python scripts/strategy-governance.py rollback --actor owner --generation CURRENT --target-generation PREVIOUS --reason "原因"
```

默认 env-file 为 `G:/StockPlatform/config/runtime.env`，不会输出其内容。人工输入的完整批准短语包括 ID/revision/hash；自动化管道/无交互 stdin 被拒绝。这是产品操作边界，不是能区分真人与控制终端 agent 的硬件认证。

## 排序配置生效与回退

`resolve_active_config(database)` 返回 None 或 `{generation, config, artifact_hash, code_hash, status}`。
只有 `status=active` 可进入正式默认设置；显式单次 settings/profile 不应被覆盖。代码变化后标记 `stale_code`，不得默默把旧实验批准沿用到新代码。
配置代次0表示尚未治理启用的真实环境配置，不意味着空配置。第一次启用保存 `previous_config`，回退到0使用此快照，保留用户原有配置。新代次追加，不删除旧批准。激活前复核测量工件仍存在/未变化。

## 自动工作队列边界

`claim_work` 一次只取一项，最多扫描50条，租约默认20分钟（最大60），SKIP LOCKED避免重复领工。不启动模型、不做自签审核；角色调度由外部受控调度器处理。
validator只领 comparison=passed 的 observing。insufficient/failed不能被反复交给validator“重试到通过”。evaluator重复领 observing须显式 `include_waiting=True`，由上游确认有新证据后触发，不能空转。

## 验收

纯规则/边界：`python -m unittest tests.test_strategy_governance tests.test_strategy_governance_boundaries -v`。
真实G盘DB：迁移后设置 `RUN_GOVERNANCE_DB_TESTS=1`，运行 `tests.test_strategy_governance_live`。该验收在一个外层回滚事务内实际写入/读回、检查旧修订拒绝、测量文件、完整状态转换、ready无生效、配置激活及回退、8条审计事件；测试配置不会对外提交。
还必须由总验收者核对真实HTTP/网页、正式/实验隔离、人工最终步骤以及自动调度。单元测试通过不得替代这些验收。

## 确定性排序配置实验宿主

`experiment_runner.py` 只调用纯 `screen`，对同一份冻结输入分别使用基线和候选配置，不由模型填写 metrics，不调用券商、不采集新数据、不改变正式配置。

```powershell
python scripts/run-governance-experiment.py prepare --input frozen-input.json --candidate-profile profile.json --allowed-strategy accumulation --output proposed-context.json
python scripts/run-governance-experiment.py run --input frozen-input.json --item-id ISSUE_ID --output G:/StockPlatform/data/research/UNIQUE_EXPERIMENT
```

prepare实际获取当前代码/配置/代次、输入**文件字节SHA256**、交易日来源，生成待独立设计者审核的上下文。不得与旧验证脚本的不同JSON序列化hash混用。run要求事项已经进入experiment/observing且spec精确匹配；代码实验或未知指标明确拒绝。

输出 baseline/candidate 完整计算结果、measurement.json、evidence.json、receipt.json；目录必须空，文件排他创建，不覆盖旧实验。evidence可直接供独立evaluator提交observe，仍需validator独立复核。

当前确定性指标：候选资格变化数、声明范围外变化数、新增买入授权数、候选数、排名变化数、计算失败数。它们验证**工程行为**，不证明排序因子提高收益。单份9月11日快照即使输入包含此前11天，只计1个独立观察交易日；候选数不当作独立交易日期。

2026-09-11真实验收保存于 `G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance/factor-experiment-6b2de295`：真实输入、123候选前后相同、28条排名改变、范围外变化0、买入授权0。数据库完整创建至observe在一个外层回滚事务执行，没有正式策略启用或持久测试事项。此结果不能称未来收益验证通过。

## 原始证据与可见等待（0093）

观察器现在将同轮有界原始候选/覆盖率与实际检测源码摘录归档到
`G:/StockPlatform/data/research/governance-evidence`。文件内容寻址且脱敏，最多80KB；
`review_evidence` 包含路径、SHA256和唯一 `issue_key=item.issue.dedupe_key`。
独立reviewer启动前由主机验证事项绑定、实际文件/hash、原始数据字段、代码行及源码hash。
路径/结论文字不算原始证据；缺证据直接等待，不启动模型。显式传入的packet也不能绕过结构和事项检查。

补充证据只能给discovered追加修订，已审核事项需要独立rework，不能悄悄替换其已审材料。
固定旧release不允许用current代替。真实案例和构造反例分别标记，未发现实际样本时明确missing，不捏造通过。

0093 `strategy_governance_diagnostics` 记录最新等待/失败、系统负责角色、下一步、证据hash、是否启动模型；
相同事项修订/原因/证据去重，不改变策略状态与修订，HTTP直接读DB而非私密日志路径。
本轮真实验收 `evidence-diagnostic-acceptance.json` 核实8事项的异步DB投影、重复等待无新增、状态不变；
PV03/PV04/PV08原始证据可复核，PV01/PV02/PV05/PV07缺对应实际反例，PV06已审核因此原记录没有被覆盖。

## 原子配置提案与验收分流

自动配置链新增 `reviewed → proposer → proposed → 独立designer → designed`。
只有明确 `issue.change_kind=ranking_config`、单一合法策略scope、可信冻结输入才能进入proposer；
代码/数据问题不会被转换为小盘因子提案。proposer只能输出一个small_cap启用/权重/单策略提案，
不得提交文件路径、哈希、度量或正式启用。主机重建配置并拒绝越界、零效果、额外改动或当前配置格式无法独立表达的修改。

提案目的由原事项 `proposal_purpose` 锁定，缺省为predictive。
只有有明确 `preference_authorization={source:user,reference:用户要求记录}` 才能走preference；模型不能自行降级目的。
preference可完成工程验收并以“工程偏好配置”入人工队列，不宣称收益提高。
predictive即使工程不变量通过，也必须等待effectiveness类独立样本/OOS证据，不能进入完整验证通过；
当前确定性宿主尚未提供收益/OOS执行器，等待是明确能力边界，不会伪造指标补齐。

真实 `proposal-live-acceptance.json` 验证了原子提案落DB、proposer不能自设计批准、另一designer成功、
正式配置不变；全部测试记录处在外层回滚事务内。

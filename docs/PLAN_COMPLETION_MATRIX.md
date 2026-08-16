# 量化与分析师联合系统计划完成矩阵

更新时间：2026-08-16。本文是四份主计划的当前状态索引，不把历史数据、回放样本或统计晋级缺口伪装成完成。

## 状态定义

- **已完成**：代码、测试和运行态均有直接证据。
- **研究中**：在线证据已落库，但统计门禁尚未满足；不影响实时规则、不自动调参。
- **暂停**：需要用户明确授权或外部资源，当前不执行。
- **工程余项**：不改变策略结论的工程加固，允许继续渐进推进。

## P0 数据与因果正确性

| 项目 | 状态 | 当前证据 |
| --- | --- | --- |
| 复权研究价与原始执行价分离 | 已完成止血 | `app/research_prices.py`；生产特征、盘后结构和 factor lab 使用 `research_*`；缺因子显式阻断 |
| ST、停牌、涨跌停和时区门禁 | 已完成 | `P0_DATA_CORRECTNESS_STATUS.md`；四种涨跌停规则、上海日期和 `upsert_bar` SQL 回归。实时 `live_policy` 与纸面成交共用 `ashare_reality.price_limit_state`：优先精确 `stk_limit`，缺位才按主板/创业科创/北交/ST 的正确价格带兜底 |
| 实时市场/数据/纸面风险 gate | 已完成 | `app/live_policy.py`、`app/paper_portfolio.py`；risk-off、质量、T+1、日亏、回撤、单票和板块集中度均可解释阻断 |
| 盘后同日完成语义 | 已完成 | latest-attempt/latest-completed 分离及回归测试 |
| 分析师唯一 promotion registry | 已完成（默认零权重） | `app/analyst_promotion.py`；未人工批准永远 `weight=0` |
| 盘中固定期限结算 | 已完成修复 | 5/15/30m 只读同一连续竞价段、目标后 90 秒内的本地腾讯报价；盘后重算仍读取原始有界窗口，午休/隔夜明确 unavailable |
| 安强作者时点动作复盘 | 已完成（仅 replay） | `author-stated-local-quote-session-bounded-replay-v1` 独立账本；不进入 live 因子、权重或飞书决策 |

## P1 运行工程

| 项目 | 状态 | 当前证据 |
| --- | --- | --- |
| provider registry、共享限频、有限重试、熔断 | 已完成 | Tushare/公共源/远端分析师文本源均使用生命周期 HTTP client；远端每次触发只短暂附加 Bearer，不在连接池、日志或状态中保留凭据；Retry-After、跨副本预约和 health 均已接入 |
| 成功/失败延迟与错误脱敏 | 已完成主要路径 | `provider_health.py`；Tushare、腾讯、Sina、东财、AKShare、巨潮公告、BaoStock、Super GET 主要路径耗时进入 health/Prometheus；兼容路径允许延迟缺省且不覆盖已有值 |
| 盘中调度、租约、outbox、飞书恢复 | 已完成 | 开盘预检、`runtime_leases`、投递回执和连续失败治理 |
| n8n 孤儿执行审计收口 | 已完成 | `scripts/reconcile-stale-n8n-executions.sh` 只标记超过 10 分钟仍为 `running` 的记录为 `crashed`，绝不删除执行证据；LaunchAgent 每 15 分钟运行一次。无候选时脚本不再创建备份目录，避免运行日志/审计目录无界累积 |
| 存储/备份/恢复前校验 | 已完成 | 总研究空间**硬上限** 40 GiB、热库**硬上限** 28 GiB；80% 预警、90% 暂停非必要高频采集。每日 PostgreSQL/workflow 备份除 14 天保留和同日去重外，另有 8 GiB 容量上限，只会回收严格命名的旧完成日备份；开盘预检同时校验该容量、`pg_restore -l` manifest 和 workflow JSON |
| 纸面组合展示与风险阻断 | 已完成 | 前端展示净值、总/净暴露、回撤、可卖量、板块暴露和风险事件；成员按观察日点时映射；新 entry 受日亏/回撤/集中度限制 |
| 策略族级健康/漂移投影 | 已完成（研究监控） | `/api/v1/strategy/health` 按策略族聚合事件和去重 episode；仅显示门禁/运营建议，不调阈值、不变更分析师权重 |
| 观察池实时覆盖上限 | 已完成 | 40 只为已核验腾讯批量盘口上限；第 41 只起扫描 fail-closed 并落 `watchlist_capacity`，不会静默截断 |
| 实时报价来源与时间戳门禁 | 已完成 | 同轮腾讯观察池批量报价才可确认，重点窗口须在 20 秒、其他窗口 45 秒内；新浪/腾讯全 A 快照严格标为证据来源并按真实 provider 落库，不可直接推送。进入个股条件的腾讯全 A 资金流带快照年龄，超过 45 秒不得确认新的 entry；`/intraday/services/status` 与前端实时卡同时展示资金流快照状态、年龄和“可确认/仅展示”边界，过期时决策路径明确标为 degraded。Super GET 仅作可选交叉确认：新鲜且价格冲突会阻断；缺失/陈旧不会替代同轮腾讯价格的新鲜度门禁，也会随证据保存并在提醒中披露。普通时段的 Super GET `rt_min` 每轮至多验证 4 只，但游标按实际观察池长度闭环推进，36 只池在 9 轮内全部覆盖 |
| 原生 async repository | 部分完成 | 策略决策/复盘/盘后候选、策略健康、策略消融、纸面研究、事件/龙虎榜、涨停/连板模式、研究目录，以及市场快照/原始 Tushare/分钟导入/最新推荐/指标计数、研究总览、分析师成绩单和研究就绪度（含历史容量估算与 replay readiness）GET 等只读投影已使用 `AsyncDatabase`；其他读写仓储仍经有界同步执行器，健康页显示异步池水位 |
| `main.py` 完全拆分 | 工程余项 | router/read-model/纯规则已拆出；容量/覆盖度/就绪度投影、特征读取、特征快照物化、分析师文本聚合与 scorecard 重算、推荐生成、Tushare/BaoStock 日线同步、全市场股票池/全市场日线同步与归一化、THS 六类板块目录编排及单类成员同步、东方财富板块成员同步、实时资金流板块补水、THS 行业/概念资金流物化、盘后 outcome 重算、**全市场快照公开行情补充/熔断/质量落库服务**、盘中调度、秒级交叉确认及 Super GET 分钟上下文归一化/扇出、分钟特征、盘中信号规则、盘中 outcome 归因与结算、远端分析师 transport/差量同步编排及其**导入/游标写服务**、盘后一键刷新/日流水线编排、盘后模式评分、候选筛选与**候选写服务**、涨停/连板样本选择、盘后板块/LHB证据聚合、涨停模式读模型、涨停分钟形态、盘中归因标签、突破评估/确认、涨停日特征、观察池日因子和竞价时段整理已迁至独立模块；其中旧的远端消息/报告同步循环已删除，只保留 `RemoteArchiveSyncService` 的文字-only 差量路径。盘中/推荐/Tushare/BaoStock/股票池/全市场日线/THS/东方财富/outcome 旧实现暂保留为未调用兼容快照，待回放验证后再删除。仍有写服务和策略编排兼容函数待机械迁移 |

## P2 数据地基与 P3 验证

| 项目 | 状态 | 准入条件 |
| --- | --- | --- |
| 3–5 年日线、复权、停牌、涨跌停、退市点时成员 | 暂停 | 用户授权范围、provider 预算和存储预算 |
| 因子研究的点时成分与内存边界 | 已完成逻辑地基 | SQL 因子面板按 `universe_membership_history`、上市/退市日期过滤；兼容 Python 引擎仅允许不超过 250 个成分的诊断，广义全 A 在读取前 fail-closed，必须走数据库内的有界 SQL 引擎。当前历史覆盖尚未回填，故不能将该地基误作完整历史验证 |
| 历史分钟回放 | 暂停 | 本地离线分钟文件或明确回填授权；策略必须复用 live `SignalSpec` |
| 本地已录制信号事件生命周期 replay | 已完成（非价格回测） | `/api/v1/strategies/intraday/replay-recorded-events` 只读 `intraday_signal_events`；按 availability 时钟写入幂等 `input_hash`/`trace_hash`，不请求 provider、不拉历史、不拟合阈值、不生成订单 |
| T+1/涨跌停/停牌/费用/滑点回放撮合 | 基础契约已完成，验证暂停 | `ashare_reality.py` 是实时风险、纸面成交和未来回放共用的整手、T+1、停牌、不同板块/ST 涨跌停、佣金/印花税/滑点及 non-fill 纯模型；尚无获授权历史路径，故不运行历史撮合或把它当策略验证 |
| purged walk-forward、embargo、DSR/PBO | 暂停 | 至少 60 aligned days、200 独立成熟信号、每 cohort 30 条 |
| 盘中阈值重校准 | 禁止启动 | P3 样本门禁通过且样本外胜出规则基线 |

## 分析师与模型演化

| 项目 | 状态 | 当前边界 |
| --- | --- | --- |
| 报告/消息差量同步、`received_at`、版本和文字证据 | 已完成 | 不下载远端图片、音视频或媒体 URL；报告与消息支路独立 |
| 分析师观点 outcome 与专家画像 | 研究中 | 当前成熟 outcome/eligible 样本不足，权重保持零 |
| Prompt Lab champion/challenger | 研究中（未晋级） | 11,313 个候选已物化；按上海可用日做时间外留出，但金标为 0，无法比较/晋级 |
| RL / contextual bandit | 暂停 | 只能在 Phase 0–5 通过后离线 challenger，不得改 live champion |

## 当前验收证据

- quant-service：全量 Python 回归通过（包含盘中连续竞价结算、分析师 received-at 与 author-stated 双时钟、零项同步 liveness 回执、事件生命周期回放确定性与幂等、策略族健康聚合、JSON 数值归一化、时间外金标留出回归、研究/热库硬上限、资金流快照新鲜度与实际观察池长度的 `rt_min` 轮转覆盖）。
- frontend：`vue-tsc --noEmit` 和 Vite build 通过；仅有 chunk size 优化警告。
- 开盘预检：compose、数据库迁移 `20260816_0046`、10 条后台租约、共享 provider pacing、30s/10s/1s/60s 节奏、飞书和可恢复备份均通过。
- 最近提交：见当前仓库最新提交；本轮未改变策略阈值或历史数据范围，推荐生成、Tushare/BaoStock/全市场同步、THS 板块目录编排、盘中归因/规则/结算拆分、分析师同步健康校验、远端文本 transport/差量同步拆分、盘后一键刷新/日流水线编排拆分、盘后模式评分/候选筛选/证据聚合/读模型委托、竞价时段整理和研究就绪度门禁，以及点时因子成分/内存边界均已通过 **418 项** Python 回归。
- 策略实现提交均已推送到 `origin/main`；工作树中可能并行存在未纳入本轮策略提交的前端/飞书适配改动，提交时必须按文件路径精确暂存，避免将凭据或未验收改动混入。

## 2026-08-14 运行与前端收口记录

- 盘中实时监控已恢复为 quant-service 内置租约循环单点运行；旧 `quantIntradayAlerts123` n8n Cron 保持取消发布，避免与服务内扫描重复。该工作流保留带 `X-Quant-Write-Key` 的手动/故障恢复图。盘中扫描落纸面决策前已补齐 `symbol`、`observed_at` 契约；开盘预检通过，量化服务 322 项测试通过。
- n8n 本地默认使用内置 JavaScript runner（`N8N_RUNNERS_MODE` 可显式改回 `external`）；现有分析师同步图只有 HTTP 节点，Python runner 缺失警告不影响其节点。当前仍需一次“当前发布版本 + success”的正式运行证据，未把 HTTP 200 但 execution 未终态化误记为完成。
- 分析师报告/消息工作流已经拆分、凭据域名和 JSON Body 已修复；本轮又将 `workflow_entity.versionId` 与发布版本对齐并重启 n8n。两条图均保留独立 Schedule Trigger，并新增不参与定时的 Manual Trigger，供运行中 n8n 的 UI 受控验收。2026-08-16 已用现有凭据对报告/消息流各做一次有界文字-only同步，服务端均返回 `completed`；但 n8n 2.33 的隔离 CLI 进程会把自身执行行遗留为 `mode=cli/running`，即使 HTTP 节点返回成功。因此健康页只接受 `mode=trigger` 且版本等于当前发布版的终态成功，CLI 冒烟不再误报为失败或定时验收；孤儿 CLI 行仍由 `scripts/reconcile-stale-n8n-executions.sh` 保留审计后收口。零项消息轮询会记录独立的 45 日 liveness 回执，而不伪造内容游标推进。当前仍需一次“当前发布版本 + trigger success”的正式运行证据，故 P0-A1 不标为完全验收。
- 前端 `Unexpected token '<'` 已修复：adapter 补齐 `/api/research/remote-archive/messages`、`/api/research/analyst-skills`、`/api/research/analyst-research/status` 三个缺失代理，前端 JSON 解码器现在会检查非 JSON 响应并给出接口路径/状态提示，不再把 SPA HTML 当 JSON 解析。三个代理真实返回 `Content-Type: application/json`；`vue-tsc --noEmit` 与 Vite build 均通过。
- 盘后一键刷新修复验证：BaoStock 隔离同步此前因 `baostock_code` 关键字无法穿过公共源有界执行器而必然失败，现已在执行器内用 `partial` 安全转发关键字，并补充回归测试。2026-08-14 重试时 Super GET `daily_all` 成功写入 5,540 条日线，盘后策略同日完成（5,540 日线标的、5,521 个具备 15 日窗口，严格 30 日结构门槛下候选 0）；未拉取历史数据。
- 仍未完成且保持原边界：历史数据回填、分钟回放、60 日/200 signal episode 样本外验证、Prompt Lab champion/challenger 晋级、RL/contextual bandit、组合自动熔断和策略自动降级。上述项目没有因本次实时修复而改变阈值或分析师 live 权重。

## 下一次恢复条件

在用户明确授权历史数据之前，只继续做不改变研究结论的工程余项；一旦授权，先执行 P2 数据就绪审计，再开启分钟回放，最后才允许 P3 统计验证。任何未通过项继续保持 `research_only` / `descriptive_only`，不得写入 live 阈值或分析师权重。

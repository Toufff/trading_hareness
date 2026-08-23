# 量化与分析师联合系统计划完成矩阵

更新时间：2026-08-22。本文是四份主计划的当前状态索引，不把历史数据、回放样本或统计晋级缺口伪装成完成。

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
| 分析师唯一 promotion registry | 已完成（默认零权重） | `app/analyst_promotion.py`；未人工批准永远 `weight=0`；scorecard 只使用版本化远端 `analyst_claims`，不再混入已退役的本地 `analyst_signals` |
| 分析师交易日时钟统一 | 已完成 | claim outcome 入场日、研究容量与异步读模型均以 `available_at AT TIME ZONE 'Asia/Shanghai'` 取交易日；`received_at` 保持唯一 live 时钟，安强 `stated_at` 仍仅进入独立作者时点复盘 |
| 分析师报告差量同步公平性 | 已完成止血 | 每个分析师每轮均检查一页最多 100 条的**文字元数据**；`max_items` 仅限制已变化报告正文的导入数。超出预算的变化不推进游标，后续轮次必重试；不请求图片、视频或媒体 URL |
| 盘中固定期限结算 | 已完成修复 | 5/15/30m 只读同一连续竞价段、目标后 90 秒内的本地腾讯报价；盘后重算仍读取原始有界窗口，午休/隔夜明确 unavailable |
| 安强作者时点动作复盘 | 已完成（仅 replay） | `author-stated-local-quote-session-bounded-replay-v1` 独立账本；不进入 live 因子、权重或飞书决策 |

## P1 运行工程

| 项目 | 状态 | 当前证据 |
| --- | --- | --- |
| provider registry、共享限频、有限重试、熔断 | 已完成 | Tushare/公共源/远端分析师文本源均使用生命周期 HTTP client；远端每次触发只短暂附加 Bearer，不在连接池、日志或状态中保留凭据；Retry-After、跨副本预约和 health 均已接入 |
| 成功/失败延迟与错误脱敏 | 已完成主要路径 | `provider_health.py`；Tushare、腾讯、Sina、东财、AKShare、巨潮公告、BaoStock、Super GET 主要路径耗时进入 health/Prometheus；兼容路径允许延迟缺省且不覆盖已有值 |
| 盘中调度、租约、outbox、飞书恢复 | 已完成 | 开盘预检、`runtime_leases`、投递回执和连续失败治理；观察池上一同源报价、EAC 首次确认、最近 61 根复权日线因子及 `标的×分钟桶` 历史量能基线均按一轮批量读取，避免随标的数线性增加小查询。当前 36 只启用观察股的批量/原单标的日因子逐项只读对照一致 |
| n8n 孤儿执行审计收口 | 已完成 | `scripts/reconcile-stale-n8n-executions.sh` 只标记超过 10 分钟仍为 `running` 的记录为 `crashed`，绝不删除执行证据；LaunchAgent 每 15 分钟运行一次。无候选时脚本不再创建备份目录，避免运行日志/审计目录无界累积 |
| 盘后一键刷新跨进程恢复回执 | 已完成 | `post-close-refresh:{stage}:{trade_date}` 通过唯一 `automation_runs` 回执恢复：完成阶段不重跑且返回 `resumed_from_receipt`，partial/blocked/failed 才重试同一行；真实 PostgreSQL JSONB、终态时间戳与 partial 重开均有契约测试 |
| 午盘/收盘复盘重启恢复 | 已完成 | `strategy_review_runs` 的同交易日/session 且 `report.status=completed` 作为 durable checkpoint receipt；重启后检查点不重复刷新行情、结算或评分，未完成记录仍在原两分钟窗口内重试 |
| 日终研究摘要重启恢复 | 已完成 | `strategy_day_summaries` 的 `sent`/`disabled`/`suppressed` 是终态回执，重启后不重建摘要；`pending`/`failed` 仍在原 19:15–19:30 窗口重试，且不向飞书推送候选池 |
| 常驻循环锁与生命周期可观测性 | 已完成 | durable `runtime_leases` 继续作为跨进程持锁真源；`/health.runtime_loops` 增加本进程 worker 的 running/waiting/lease-lost/backoff/error 生命周期状态，解释未启动/交接/异常退出而不把它冒充为业务心跳，也不记录 provider payload 或凭据 |
| 存储/备份/恢复前校验 | 已完成 | 总研究空间**硬上限** 40 GiB、日频 P2 证据热库**硬上限** 36 GiB（为受限历史保留 4 GiB artifact 余量）；80% 预警、90% 暂停非必要高频采集。每日 PostgreSQL/workflow 备份除 14 天保留和同日去重外，另有 8 GiB 容量上限，只会回收严格命名的旧完成日备份；开盘预检同时校验该容量、`pg_restore -l` manifest 和 workflow JSON |
| 纸面组合展示与风险阻断 | 已完成 | 前端展示净值、总/净暴露、回撤、可卖量、板块暴露和风险事件；成员按观察日点时映射；新 entry 受日亏/回撤/集中度限制 |
| 策略族级健康/漂移投影 | 已完成（研究监控） | `/api/v1/strategy/health` 按策略族聚合事件和去重 episode；仅显示门禁/运营建议，不调阈值、不变更分析师权重 |
| 版本化 FactorSpec 与 episode 契约 | 已完成（证据/shadow） | `strategy_contracts.py` / `intraday_factor_contracts.py`；每个已登记盘中因子携带版本、输入、时钟、质量门禁、训练/推理许可和弃用日期。当前 `training_permitted=false`，只能进证据和归因，不能接入实时评分 |
| 持久化 SignalSpec / Insight 证据契约 | 已完成（证据/shadow） | 扫描器在写入事件前把策略族、版本、30m 结算期限、5 分钟有效期、稳定失效原因码、证据引用与风险旗标写入 `conditions.signal_contract`；不改变评分、提醒阈值或订单边界 |
| 观察池实时覆盖上限 | 已完成 | 40 只为已核验腾讯批量盘口上限；第 41 只起扫描 fail-closed 并落 `watchlist_capacity`，不会静默截断 |
| 盘中规则输入快照与留存 | 已完成（为未来回放采证） | 每轮每只观察股在调用纯 `signal_rules` 前，将最小 watch/同刻报价/前帧/日因子/分钟特征/精确同伴上下文冻结为 `intraday_rule_input_snapshots`，无信号行也记录；不保存重复 provider raw。默认留存 90 天（下限 60、上限 120），同源观察池报价同步裁至该窗口；盘口/秒级交叉确认仍按其独立短窗口保留。重复的 `suppressed/confirming` 事件也在 90 天后才有界清理，且仅当无投递/无 outcome；`confirmed/alerted`、回执、纸面决策和结算证据永不由该清理删除。只积累未来实时证据，不补拉历史、不改变 live 阈值 |
| 实时报价来源与时间戳门禁 | 已完成 | 同轮腾讯观察池批量报价才可确认，重点窗口须在 20 秒、其他窗口 45 秒内；新浪/腾讯全 A 快照严格标为证据来源并按真实 provider 落库，不可直接推送。进入个股条件的腾讯全 A 资金流带快照年龄，超过 45 秒不得确认新的 entry；`/intraday/services/status` 与前端实时卡同时展示资金流快照状态、年龄和“可确认/仅展示”边界，过期时决策路径明确标为 degraded。Super GET 仅作可选交叉确认：新鲜且价格冲突会阻断；缺失/陈旧不会替代同轮腾讯价格的新鲜度门禁，也会随证据保存并在提醒中披露。普通时段的 Super GET `rt_min` 每轮至多验证 4 只，但游标按实际观察池长度闭环推进，36 只池在 9 轮内全部覆盖 |
| 全 A 横截面并发缓存 | 已完成 | `SharedAsyncSnapshot` 将腾讯全 A 资金流横截面限定为 30 秒 TTL：并发扫描只共用一条在途上游请求，失败不写成新鲜缓存；观察池仍独立走 40 只以内腾讯批量报价，避免慢横截面拖慢 10 秒窗口 |
| THS 概念精确成员恢复 | 已完成 | 成员同步仅接受 `ths_member` 的精确代码关系；“全部 provider 临时熔断”失败在线路冷却后可受限恢复，格式/截断等失败仍保留三次上限。2026-08-16 对 2026-08-14 的唯一遗留 `885338.TI` 恢复成功，概念覆盖为 **387/387**、成员为 70,998；未做中文名称猜测 |
| 原生 async repository | 部分完成 | 策略决策/复盘/盘后候选、策略健康、策略消融、纸面研究、事件/龙虎榜、涨停/连板模式、研究目录，以及市场快照/原始 Tushare/分钟导入/最新推荐/指标计数、研究总览、**Agent 自动化回执、市场资金流特征、全部板块/概念/精确成员证据、涨停联动候选、Prompt Lab 状态、已落库分析师市场复盘、有界市场评测、分钟时间轴、分析师研究状态及归档游标/总览、提供者目录/能力/健康、PIT 文本因子**等 GET 已使用 `AsyncDatabase`；AST 回归要求其余面板 GET 不能回退为同步 DB 路径。保留的 3 个同步 GET 是明确运维例外：静态 Agent/路线图、注入式盘中状态兼容分支；跨 n8n public schema 的同步健康已成为 async 路由但暂经有界执行器。其余读写仓储仍经有界同步执行器，健康页显示异步池水位 |
| `main.py` 完全拆分 | 工程余项 | router/read-model/纯规则已拆出；盘中侧现进一步分离了观察池报价采集、扫描总编排、单事务信号持久化、全板块成员批次与 THS 概念成员恢复。容量/覆盖度/就绪度、单股窗口/claim 证据、单股多源研究、特征读取、分析师文本/成绩单门禁、概念涨停候选持久化、日线/板块同步、盘后编排、盘口/分钟/板块曲线、远端文字同步、本地研究实验/快照、Tushare fetch ledger 和研究维护写入均有独立模块；旧 n8n 直写分析师信号入口已删除。主文件当前约 5,130 行；仍保留明确的 composition root、兼容入口和少量写服务，旧兼容快照待获授权的回放验证后再删除。 |

## P2 数据地基与 P3 验证

| 项目 | 状态 | 准入条件 |
| --- | --- | --- |
| 三年日线、复权、停牌、涨跌停与日频板块/龙虎榜证据 | 日线 P2 已暂停；板块/龙虎榜历史待独立覆盖审计 | 截至 2026-08-17，`daily`、`adj_factor`、`daily_basic`、`stk_limit`、`suspend_d` 有 505 个完整全市场横截面日，覆盖 2023-08-15 至 2026-08-14（1,095 个日历日）。为优先保护盘中观察，第三年历史任务已暂停且仅保留完成检查点；不得由服务或 n8n 自动恢复。还差 215 个完整日频截面，且仍缺带供应商 `source_available_at` 的离线分钟证据。每条日频事实保留 `ingested_at`，策略 `available_at` 明确标为 `assumed_eod_1700_asia_shanghai_v1`，不是供应商发布时间。历史行业/概念资金流、龙虎榜和指数仍不因缺失而伪报完成。 |
| 因子研究的点时成分与内存边界 | 已完成逻辑地基 | SQL 因子面板按 `universe_membership_history`、上市/退市日期过滤；兼容 Python 引擎仅允许不超过 250 个成分的诊断，广义全 A 在读取前 fail-closed，必须走数据库内的有界 SQL 引擎。当前历史覆盖尚未回填，故不能将该地基误作完整历史验证 |
| 历史分钟回放 | 因果时间合同已完成；价格路径回放暂停 | 本地分钟行现区分 `bar_time`（K 线收盘时刻）、`source_available_at`（供应商记录的可用时刻）和本地 `available_at`（导入时刻）。没有前者的文件不得进入回放；`offline_minute_bar` 只构造按来源可用时间排序的确定性事件，不会伪造同刻报价/板块/因子输入或重跑价格规则。仍需具备来源可用时钟的本地文件或明确回填授权，以及复用 live `SignalSpec` 的完整冻结证据包 |
| 未来盘中规则回放证据 | 已完成采证与一致性重放基础，验证未开始 | `intraday_rule_input_snapshots` 的 v2 合同冻结核心规则与同刻 policy/risk gate 输入；旧 v1 仅兼容 core-only。`/api/v1/strategies/intraday/replay-recorded-inputs` 无 provider、历史导入、阈值拟合或订单能力，并明确排除 event state、执行和收益结论。数据有 60–120 天有界留存，只从上线后的真实扫描累积，不改变既有事件、不可替代获授权的历史分钟数据或完整市场横截面 |
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

- quant-service：当前构建后的全量 **686** 项 Python 回归通过。除历史的 v2 规则输入、policy/risk gate 与作者时点回放边界外，新增覆盖观察池报价的直连/兜底边界、扫描总编排的闭市零外呼和确认投递、单事务信号证据顺序、THS/东财精确成员批次失败隔离、概念目录不可用时成员恢复 fail-closed、概念/涨停池精确代码 join，以及原生异步自动化回执、市场资金流、板块/概念/精确成员、涨停联动、Prompt Lab、已落库分析师市场复盘、有界市场评测、分钟时间轴、分析师研究状态、归档总览/游标、提供者目录/能力/健康、PIT 文本因子、路由 async 边界、真实 `upsert_bar` SQL 集成、单股窗口/claim 就绪度、决策卡、板块曲线/复盘/轮动/挖掘、分析师操作回放/outcome、技能/研究/归档证据读取，以及本地研究窗口/因子拒绝、快照控制面阻断、Tushare ledger 缓存/取消、研究维护和远端 claim 唯一成绩单来源边界。
- frontend：2026-08-22 已重新执行 `npm run api:check`、`vue-tsc --noEmit` 与 Vite production build，均通过；概念成员卡现在区分有效精确映射覆盖和同日同步回执，避免显示层掩盖证据差异。生成 API 类型仍与 OpenAPI 一致。构建仅保留 charts/element-plus 大于 500 kB 的优化警告，不影响功能或接口契约。
- 开盘预检：compose、数据库迁移 `20260822_0055`、必需后台租约、共享 provider pacing、30s/10s/1s/60s 节奏、飞书、当前发布版分析师文字同步以及可恢复备份均通过；预检是只读的，不请求市场 provider 或发送提醒。
- 最近提交：见当前仓库最新提交；本轮未改变策略阈值、live 权重或历史数据范围。所有新增拆分均保持既有 provider、数据库事务和告警边界。
- 策略实现提交均已推送到 `origin/main`；工作树中可能并行存在未纳入本轮策略提交的前端/飞书适配改动，提交时必须按文件路径精确暂存，避免将凭据或未验收改动混入。

## 2026-08-14 运行与前端收口记录

- 盘中实时监控已恢复为 quant-service 内置租约循环单点运行；旧 `quantIntradayAlerts123` n8n Cron 保持取消发布，避免与服务内扫描重复。该工作流保留带 `X-Quant-Write-Key` 的手动/故障恢复图。盘中扫描落纸面决策前已补齐 `symbol`、`observed_at` 契约；开盘预检通过，量化服务 322 项测试通过。
- n8n 本地默认使用内置 JavaScript runner（`N8N_RUNNERS_MODE` 可显式改回 `external`）；现有分析师同步图只有 HTTP 节点，Python runner 缺失警告不影响其节点。当前仍需一次“当前发布版本 + success”的正式运行证据，未把 HTTP 200 但 execution 未终态化误记为完成。
- 分析师报告/消息工作流已经拆分、凭据域名和 JSON Body 已修复；本轮又将 `workflow_entity.versionId` 与发布版本对齐并重启 n8n。两条图均保留独立 Schedule Trigger，并新增不参与定时的 Manual Trigger，供运行中 n8n 的 UI 受控验收。2026-08-16 已用现有凭据对报告/消息流各做一次有界文字-only同步，服务端均返回 `completed`；但 n8n 2.33 的隔离 CLI 进程会把自身执行行遗留为 `mode=cli/running`，即使 HTTP 节点返回成功。因此健康页只接受 `mode=trigger` 且版本等于当前发布版的终态成功，CLI 冒烟不再误报为失败或定时验收；孤儿 CLI 行仍由 `scripts/reconcile-stale-n8n-executions.sh` 保留审计后收口。零项消息轮询会记录独立的 45 日 liveness 回执，而不伪造内容游标推进。当前仍需一次“当前发布版本 + trigger success”的正式运行证据，故 P0-A1 不标为完全验收。
- 前端 `Unexpected token '<'` 已修复：adapter 补齐 `/api/research/remote-archive/messages`、`/api/research/analyst-skills`、`/api/research/analyst-research/status` 三个缺失代理，前端 JSON 解码器现在会检查非 JSON 响应并给出接口路径/状态提示，不再把 SPA HTML 当 JSON 解析。三个代理真实返回 `Content-Type: application/json`；`vue-tsc --noEmit` 与 Vite build 均通过。
- 盘后一键刷新修复验证：BaoStock 隔离同步此前因 `baostock_code` 关键字无法穿过公共源有界执行器而必然失败，现已在执行器内用 `partial` 安全转发关键字，并补充回归测试。2026-08-14 重试时 Super GET `daily_all` 成功写入 5,540 条日线，盘后策略同日完成（5,540 日线标的、5,521 个具备 15 日窗口，严格 30 日结构门槛下候选 0）；未拉取历史数据。
- 仍未完成且保持原边界：3–5 年扩展历史、分钟路径回放、60 日/200 signal episode 样本外验证、Prompt Lab champion/challenger 晋级、RL/contextual bandit、组合自动熔断和策略自动降级。一年日频 PIT 修复不构成分钟回放或阈值调参授权，也没有改变分析师 live 权重。

## 2026-08-16 收口记录

- 运行态复核：所有服务容器健康；实时服务在非连续竞价时正确 `standby`，并保留 36 只启用观察股的 30 秒/特别窗口 10 秒/盘口 3 秒/板块曲线 60 秒节奏。主 Tushare 明确标为无实时能力；Super SDK 与 Super GET 均按已验证协议分工，代理连接池和 Super GET 线程内 `requests.Session` 复用已启用。
- 存储治理复核：量化 schema 13.39 GB，占 40 GiB 总研究预算 31.2%、28 GiB 热库预算 44.5%；7 天盘口与秒级交叉确认、60 天板块曲线/轮动、90 天分钟剖面及 60–120 天（默认 90）规则输入/观察池报价均有留存边界。未删除原始证据，也未启动历史回填。
- 订单簿数据只作为 `attribution_only`：`qi5`、窗口聚合 order-flow proxy、封单侵蚀等已入 SignalSpec 证据引用；不改变实时评分、阈值或提醒资格。
- 盘中规则输入回放已升级为 `intraday-rule-input-replay-v2`：新扫描冻结市场状态、Super/Tencent 交叉确认、纸面仓位与组合风险上下文，回放可调用同一纯函数 policy/risk gate；旧 v1 快照明确标记为 `core-only`，不会伪装成完整门禁回放。当前 2026-08-17 已存 3,996 条快照均来自 v2 部署前，故本次回放的 `policy_replayable_snapshots=0`；下一交易时段才会产生可验证的 v2 样本。
- 分析师链复核：报告与消息流各自拥有持久 cursor 与 45 天 liveness receipt；最近的服务端文字-only 增量均已完成。当前 n8n 当前发布版还在等待下一个工作日的首个 `trigger/success` 运行，健康页明确显示该状态，不把旧版或 CLI 执行行当作正式调度成功。
- 上海日期回归：对晚 UTC 的分析师消息，结算入场必须发生在其对应的上海交易日之后；该语义已覆盖 local-only outcome recomputation，容量/readiness 投影与兼容实现，并有回归测试。

## 2026-08-16 P2 日频执行记录

- 先以真实 provider 结果修复 2025-06-13 至 2025-08-14 的 45 个开市日：45/45 完成、0 逻辑失败。`daily` 243,081 行、`adj_factor` 242,646 行、涨跌停 242,766 行；最小单日股票覆盖 5,384，复权平均覆盖 99.95%，涨跌停平均覆盖 100%。旧主源 DNS/TLS 失败没有被标成完成，City/Super SDK 的成功记录按日期写入并参与 canonical 控制回填。
- 随后对 2024-08-15 至 2025-08-14 完成 242/242 缺口审计，0 逻辑失败，并补齐旧批次少量缺失的日线控制截面。复权/涨跌停重联现按 `tushare_super_sdk → tushare_super_get → tushare_primary` 的已验证记录选择，且限制目标交易日，避免旧主源硬编码或年度检查扫描全库。
- 当前 `canonical_bars_daily` 覆盖 2023-08-15 至 2026-08-14、505 个全市场横截面日，已满足 1,090 个日历日跨度，但 P2 仍明确阻断：还需要 215 个完整日截面和带供应商 `source_available_at` 的离线分钟数据。2026-08-17 已按“盘中观察优先”暂停第三年日频任务；绝不把本地导入时间冒充分钟来源可用时钟，也不因此改变实时策略阈值。
- 日频回填遵守 40 GiB 总研究空间 / 36 GiB 热库硬上限。暂停时数据库约 23.6 GiB（热库约 65.5%）；80% 预警或 90% 暂停会优先保护实时服务和已有证据。

## 下一次恢复条件

第三年日频任务已暂停，并保留 `quant.fetch_runs` 的 `annual:*` 完成检查点；只能在用户明确恢复且不影响盘中观察时续跑。恢复后必须重查 720 日/1,090 日 P2 门禁和 80% 存储预警。仍需取得带来源可用时钟的分钟数据并通过 P2 数据就绪审计，才可能开启分钟回放或 P3 统计验证。任何未通过项继续保持 `research_only` / `descriptive_only`，不得写入 live 阈值或分析师权重。

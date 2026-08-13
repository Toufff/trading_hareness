# 策略与分析师联合量化系统：二次架构审计与实施计划

日期：2026-08-13（Asia/Shanghai）  
状态：实施蓝图，`research_only`  
范围：盘中/盘后策略、分析师点时因子、验证、纸面组合、前端与运行治理  
明确边界：本轮只读检查本地已有数据与运行状态，**没有拉取任何历史行情，也不授权自动下单**。

## 1. 结论先行

现有系统不需要推倒重来。它已经具备个人研究平台里最难得的几块地基：原始证据到结算的分层、点时可用性、显式观察池、提醒状态机、板块精确成员映射、小样本门禁，以及分析师观点的 Sleeping Experts 研究框架。

当前瓶颈也不是“因子太少”，而是下面四件事还没有闭环：

1. **复权研究视图与实时准入已完成止血，但覆盖门禁仍在。** 生产推荐、盘后结构和 factor lab 共用显式复权研究价；缺 `adj_factor` 的跨日特征会标记为质量阻断，实时风险由 `policy_gate` 在事件确认前再次裁决。真实全市场控制面覆盖仍不足，不能据短样本晋级。
2. **分析师同步已拆流并完成配置修复，消息流尚未有成功游标。** 报告流按版本/哈希差量；消息流改用远端签名的全局 `/messages/updates` 增量流，每轮最多 100 条，不再使用 n8n 内置分页，成功导入整页后才推进不透明 cursor。此前验证发现游标字段名与远端列表 URL 不一致，导致请求落到 `/analysts/undefined/messages`；又发现旧分页节点有 OOM 风险，均已修复并于 2026-08-13 盘后重新发布。n8n 主进程与外置 runner 已注入所需运行环境，但连续 10 轮 0 个 429 的验收仍待下一调度窗口。
3. **统计门禁在正确地阻止晋级。** 本地只有 16/732 个完整横截面日、0/60 个历史分钟回放日、154/200 个确认事件；109 条已成熟信号记录只跨 3 个交易日，且尚未经过 episode 去重，不能称作独立样本。分析师则是 0 个 eligible 观点、0 个成熟观点结果。
4. **从信号到组合仍缺一层。** 目前系统能发现、提醒、结算，但还没有统一的纸面订单、仓位、行业暴露、T+1 可卖量、组合回撤和策略预算模型。

因此正确路线是：

```text
先修正确性与分析师同步
  -> 统一事件/策略/观点契约
  -> 建立 episode 与纸面组合
  -> 前向积累并做市场-only基线
  -> 分析师做增量、消融和专家权重研究
  -> 经回放、样本外和纸面门禁后人工晋级
  -> RL 永远最后，且只作受约束 challenger
```

## 2. 现场审计快照

### 2.1 策略与运行

| 项目 | 现场值 | 判断 |
|---|---:|---|
| 注册策略 / 注册因子 | 6 / 13 | 已有研究目录，但策略契约不够统一 |
| 盘中事件 | 3,427 条、20 只股票、4 个自然日 | 工程样本已有，统计时间跨度不足 |
| 确认或已提醒事件 | 154 | 未达到 200 门槛 |
| 已成熟信号记录 | 109，跨 3 个交易日，尚未 episode 去重 | 不能作为独立样本门槛 |
| 当日飞书观察池投递 | 43 sent / 0 failed | 提醒链路运行良好 |
| 观察池 | 24 条、23 条启用 | 显式观察与自动挖掘保持隔离 |
| 完整横截面日 | 16 / 732 | P2 数据门禁未通过 |
| 历史分钟回放日 | 0 / 60 | P3 回放门禁未通过 |
| canonical 日线 | 89,115 行 / 5,547 标的 / 34 日 | 横截面够宽，时间不够长 |
| 有 `adj_factor` / `limit_up` 的日线 | 各 187 行（0.21%） | 生产特征不能默认认为复权/控制面完整 |
| `is_suspended=true` | 0 行 | 停牌控制面仍需验证实际覆盖 |
| 研究库大小 | 1.247 GiB | 当前可控 |
| quant / PostgreSQL 内存 | 约 224 / 197 MiB | 当前可控，离线研究不应塞入实时进程 |

盘中固定期限结果目前仍是小样本描述：5m、15m、30m、收盘和次日收盘的总体平均方向收益均未形成稳定正优势，其中次日收盘只有 27 个成熟样本且明显为负。因此不能据此调阈值，更不能把当天复盘称作 RL。

### 2.2 分析师链

| 项目 | 现场值 | 判断 |
|---|---:|---|
| 分析师 / 日报 | 5 / 53 | 已有多作者基础 |
| evidence / claims | 532 / 484 | 结构化证据已有规模 |
| 观点 | 109：89 replay_only、20 neutral、0 eligible | 当前不得进入实时权重 |
| 观点结果 | 872：856 pending、16 unavailable、0 matured | 专家权重无合法奖励样本 |
| 安强动作 | 77 条、跨 3 日、17 标的 | 只能复盘；5 分钟内点时可用样本为 0 |
| 新消息表 | 当前仍无成功游标 | 不能解释为远端无消息；健康页明确标记消息流 `never_succeeded`，等待真实调度验证 |
| skill profiles | 54 个版本、5 位作者 | 目前是描述卡，不是学习后的技能模型 |
| provenance profiles | 0 | 独立性、推广属性、受众规模不能参与先验 |
| 专家研究运行 | 1，`research_only` | 正确地没有实时影响 |
| n8n 同步 | 当日 10 次均 error | P0 运行事故 |

### 2.3 已经做对的设计

- `received_at == strategy_available_at` 是远端消息唯一策略时间；`published_at`、`edited_at`、`stated_at` 只供审计或复盘。
- 远端只同步已提取文字，不拉取图片、音视频和媒体 URL。
- 盘中信号有二次确认、冷却、durable outbox、失败重试、恢复回执和决策卡链接。
- 自动挖掘候选只进入前端研究层，不自动加入观察池，也不通过飞书逐只轰炸。
- 板块候选依赖精确 THS 成员，不用名称猜归属。
- 分析师先跑等权基线，再做 Fixed-Share；未达到日期簇和显著性门槛时权重为零。
- `contextual_policy_learning` 明确是离线复核，不修改实时规则。

## 3. 关键缺口与优先级

### P0：会污染结果或让链路失效

#### P0-S1 生产特征复权口径分裂（已完成止血，覆盖仍受门禁）

`build_feature_snapshot` 在 `quant-service/app/main.py:970` 直接使用 raw `close` 计算均线和动量；`factor_lab.py:40` 已有正确的“canonical 保留原价、研究视图显式乘复权因子”边界。两者必须统一，否则除权日会产生伪动量、伪波动和伪横盘破位。

处理：抽出唯一 `research_adjusted_price_view`，供推荐、盘后结构和 factor lab 共用。`adj_factor` 缺失时必须产生质量标记，不能静默把不同口径混合。

鉴于当前复权因子仅覆盖 0.21%，P0 不回填历史，也不能简单把全市场切到“强制复权”。过渡契约是：

- 日内价格、盘口、涨跌停和撮合继续使用 canonical 原价；
- 跨日收益、均线和横盘结构只有在所需回看窗口的复权因子完整时才可 `decision_eligible`；
- 因子缺失时保留原价描述和 `adj_factor_missing` 证据，但相关跨日特征不得贡献入场分数或确认提醒；
- 已知公司行为或机械跳空落入回看窗口时直接标 `corporate_action_unresolved`，不得用 raw 连续性猜测；
- P0 用合成除权夹具和本地已有完整样本验收；真实全市场覆盖率验收留到用户授权的数据阶段。

#### P0-S2 实时市场与数据风险没有成为准入门禁（已完成）

当前规则先生成信号，市场状态更多用于归因。P0 先在“规则命中”和“事件确认”之间增加不依赖持仓账本的纯函数 `policy_gate`：

- 市场状态：广泛风险关闭时禁止新增 entry，但保留 watch 证据；
- 数据新鲜度：任一关键输入 stale 时降级；
- 静态可交易性：停牌、当时涨跌停状态和交易时段；
- 微观结构：只作为确认/拒绝证据，不越权决定方向。

T+1 可卖量、整手、单票/板块/策略暴露、日内亏损和组合回撤依赖真实的 paper position/portfolio ledger，随 Phase 2 一起实现。P0 的 `hard_stop` 若没有可卖量证据，只能写成风险告警，不能伪装成可执行卖出。

#### P0-S3 盘后任务“当天完成”语义不严（已完成）

自动循环可能使用上一完整交易日的数据，却把本地当天记为 completed。循环必须显式传入 `as_of_date=上海当天`；读取模型同时返回 `latest_attempt` 和 `latest_completed`，不能让一个 blocked 尝试遮住上一版可用候选。

#### P0-A1 分析师同步重复全量详情导致 429（代码/部署已修，运行验收待下一窗口）

当前 `scripts/build-remote-archive-sync-workflow.mjs:25-40` 每轮翻全量报告，再逐份请求详情；报告与消息在同一工作流分叉，报告支路失败会使整体执行失败。处理：

- 报告同步与消息同步拆成两条工作流；
- 报告按 `(report_id, version, content_hash)` 差量拉详情；
- 每分析师串行、尊重 `Retry-After`、有界指数退避；
- 消息 cursor 持久化到 PostgreSQL，不依赖 n8n staticData；
- 分页扫到 cursor，处理 100+ 突发和同时间戳多消息；
- 任一支路故障不阻断另一支路，健康页分别显示。

#### P0-A2 分析师时区与历史 as-of 泄漏（已完成）

SQL 过滤使用 UTC date，而折叠日使用上海日；`_mature_outcome_rows(as_of_date)` 也没有保证 `exit_date <= as_of_date`。统一使用上海交易日或已冻结的 `opinion_date`，历史研究快照必须只看到当时已经成熟的 outcome。

#### P0-A3 新旧分析师门禁双轨（已完成，默认零权重）

旧推荐链有“两人各 30 条即可给 10%”的 scorecard 门禁，新研究链则要求 60 日期簇并有 5,000 outcome 的后续门禁。必须建立唯一的 `promotion_registry`：

- 默认 `analyst_weight=0`；
- 只有指定研究版本达到 `eligible_for_review`、通过人工批准且未发生漂移/同步故障时才允许非零；
- `replay_only`、neutral、未映射和迟到版本永远不能旁路；
- 同步故障或新模型抽取漂移时自动归零，不保留陈旧权重。

## 4. 目标架构

### 4.1 一条统一的时间事件链

```text
Source Evidence
  source_time / received_at / available_at / content_hash
            |
            v
Point-in-time Feature Snapshot
  market / sector / stock / order-book / analyst observation
            |
            v
Primary SignalSpec
  direction + setup + invalidation + evidence ids
            |
            v
Meta Gate
  freshness + regime + tradability + sector + microstructure
  + analyst delta (initially 0)
            |
            v
Signal Episode
  detected -> confirming -> confirmed -> alerted/invalidated/cleared
            |
            v
Paper Decision Proposal
  target risk, not an order; portfolio/risk model may reduce to zero
            |
            v
Outcome Ledgers
  fixed horizons + triple barrier + tradability + net cost
            |
            v
Offline Evaluation
  market-only vs market+analyst, trial registry, drift, challenger
```

任何对象都必须能回答三个问题：当时知道什么、何时可用、依据哪一个不可变版本。

### 4.2 `SignalSpec` 合约

每条策略统一为版本化配置，而不是散落的阈值：

```yaml
identity:
  strategy_key: intraday_eac_breakout
  version: research-v4
scope:
  universe: explicit_watchlist
  sessions: [continuous_auction]
inputs:
  required: [quote, minute_path, sector_context]
  optional: [order_book, analyst_context]
  max_age_seconds: {}
setup:
  direction: long
  trigger: expansion
  confirmation: acceptance
  continuation: optional
invalidation:
  price: []
  sector: []
  data_quality: []
risk:
  market_gate: true
  tradability_gate: true
  portfolio_gate: true
label:
  fixed_horizons: [5m, 15m, 30m, close, next_close]
  triple_barrier: preregistered
alert:
  audience: explicit_watchlist_only
  rearm_policy: clear_then_material_change
governance:
  status: research_only
  trial_id: immutable
```

应保留的策略族：

| 策略族 | Primary setup | Meta confirmation | 主要反证 |
|---|---|---|---|
| EAC 首次扩张 | 新高、相对量能、价格位置 | 承接、同题材广度、板块资金、盘口窗口聚合 | 快速跌回 VWAP、板块背离、数据 stale |
| 绿盘回收 / 深水反转 | 从日内低点恢复、重回关键价 | 主动流、同板块同步、二次确认 | 弱反弹无量、板块继续流出 |
| 板块轮动 | 分钟资金增量、符号翻转、排名变化 | 持续性、广度、精确成员个股扩散 | 单点尖峰、映射不完整 |
| 涨停关联 | 锚点涨停/连板、同板块关联 | 候选相对强度、量能、未过度延伸 | 候选已封板不可买、关联仅同名无成员证据 |
| 横盘蓄势 / 刚启动 | ATR/区间/成交量收敛、相对强度 | 放量突破后承接 | 除权伪跳空、上方筹码压力、板块走弱 |
| 风险/退出 | 硬风险、接受失败、板块反转 | T+1 可卖量和流动性 | 不能把“应退出”伪装成“可成交” |

新规则不得直接进入 `intraday_signal_rules`。先登记研究问题、trial、数据需求、标签、反证和 kill criterion，再进入 shadow。

### 4.3 `SignalEpisode` 代替十分钟重复告警

同一行情脉冲的多次扫描不是独立样本。新增 episode 语义：

- `episode_id`：同一标的、策略、方向、连续条件；
- `material_state_hash`：价格区间、阶段、板块状态、量能分位等离散摘要；
- `stage`：expansion / acceptance / continuation / failure；
- `clear_condition`：条件明确消失；
- `rearm_condition`：清除后重新触发，或发生显著阶段升级；
- 同 episode 的 1m/3m/5m 扫描只算一个独立样本。

飞书继续只面向显式观察池。自动挖掘 Top20 只更新前端候选，候选设置 TTL；只有人工提升到观察池，才开启策略提醒。

### 4.4 分析师统一观察模型

消息与日报最终都映射到同一个 append-only `AnalystObservation`：

| 字段组 | 字段 |
|---|---|
| 身份 | analyst_id、source_id、source_version、content_hash |
| PIT | received_at、strategy_available_at、published_at、edited_at、stated_at、time_precision |
| 对象 | market / theme / stock、精确 symbol/sector mapping、mapping_version |
| 判断 | action、direction、horizon、strength、confidence、position_intent |
| 条件 | entry_zone、target、stop、invalidation、catalyst、risk |
| 证据 | evidence_span、extractor、prompt/schema/model version、uncertainty |
| 状态 | eligible / replay_only / neutral / unmapped / rejected |

原则：

- 消息以远端不可变 `received_at` 作为唯一策略可用时间；日报以本地首次取得该不可变版本的 `remote_report_versions.first_seen_at` 作为可用时间。作者自述的时间只能用于 replay。
- 编辑后的文本产生新版本，不能回写覆盖旧预测。
- LLM 只做结构化抽取、矛盾检查和反方问题，不直接产生买卖信号。
- 不保存自由思维链作为因子；保存字段、证据片段、版本和不确定性。
- 每条预测在产生时冻结，结算后才能更新专家账本。

### 4.5 每位分析师的“技能”如何建模

技能不是模仿语言风格，而是一个分层、可结算的能力向量：

```text
analyst
  x scope (market/theme/stock)
  x action type (buy/watch/reduce/avoid)
  x horizon
  x market regime
  x evidence quality / latency bucket
```

每个单元记录 coverage、precision、方向残差收益、命中率、MFE/MAE、校准误差、风险遗漏率和日期簇。小样本按“单元 -> 分析师总体 -> 全局等权”分层收缩。

现有 Fixed-Share 保留，但做三项修正：

1. 同一观点多个 horizon 不能被当作多个独立奖励；选择预注册主 horizon 更新专家，其他期限只用于曲线诊断。
2. reward 使用正确基准的残差收益，扣除假设成本和可用延迟；主题用点时成员篮子，个股优先用行业/规模基准。
3. 先证明 `market + analyst` 在严格相同样本上优于 `market only`，再讨论权重；不能只证明某分析师自身命中率为正。

### 4.6 市场与分析师融合

近期不做一个“大一统分数”。采用可解释的两阶段结构：

1. **Primary model**：价格、量能、板块、资金和微观结构决定方向与 setup。
2. **Meta model**：只回答“本次是否值得提醒/进入纸面组合”，输出通过、观察或拒绝。
3. **Analyst delta**：作为 meta context 的消融项，初始恒为 0；通过门禁后 shadow 上限 5%，纸面阶段上限 10%，永远不能覆盖硬风控。
4. **Risk overlay**：最后应用，可把 target risk 降到 0，不能把拒绝信号变成买入。

每次实验必须同时报告：

```text
market-only
analyst-only
market + unweighted analyst consensus
market + fixed-share analyst delta
market + analyst delta + risk overlay
```

若增量只来自更高换手、热门股暴露、板块 beta 或事后报告，则判失败。

### 4.7 纸面组合层

在任何真实执行之前，增加 paper ledger：

- `paper_decisions`：信号、决策时刻、first_tradable_at、假设下单延迟；
- `paper_orders`：submitted / accepted / partially_filled / filled / cancelled / rejected；
- `paper_positions`：总量、可卖量、买入日期、成本、板块和策略归属；
- `paper_portfolio_snapshots`：净值、现金、总/净暴露、行业暴露、回撤；
- `paper_risk_events`：T+1、涨跌停、停牌、容量、日亏、回撤和数据故障。

成交现实至少包括：T+1、100 股整手、各板块/ST 涨跌停、停牌、卖出侧印花税、最低佣金、spread、时段/波动/参与率冲击、提醒延迟和 non-fill。涨停封单只能表示可观察性，不代表排板可成交。

## 5. 验证与证伪体系

### 5.1 标签

保留现有 5m/15m/30m/close/next_close 作为诊断，同时新增预注册 triple-barrier：

- 上障碍、下障碍按当时可用波动率缩放；
- 垂直障碍是最大持有时间；
- 保存先触发哪个障碍、MFE、MAE、first tradable、净成本与 non-fill；
- 具体倍数只在 trial 登记后冻结，不能看完结果再选。

### 5.2 样本独立性

- 同一 episode 的多次扫描只算一个独立事件；
- 标签区间重叠时计算 sample uniqueness；
- 训练/检验按交易日和 episode 聚类；
- 观察池结果必须与全市场/自动候选对照，避免只研究“本来就关注的强票”。

### 5.3 时间切分与多重试验

- 禁止随机 KFold；使用 walk-forward，并按标签重叠 purge、在测试窗后 embargo。
- 参数初筛可用 vectorbt，但只冻结少量候选进入事件回放。
- 所有看过的参数、因子、prompt、规则组合都登记 trial，不只记录赢家。
- 最终报告 DSR 和 PBO/CSCV；大量因子搜索不得继续用普通 `t > 2` 当发现标准。

### 5.4 评价指标

| 层 | 主指标 | 辅助/风险指标 |
|---|---|---|
| 数据 | PIT 完整率、freshness、双源价差 | 缺失率、429/5xx、延迟分布 |
| 抽取 | entity/action/horizon precision、recall | 否定/条件/风险遗漏、人工修订率 |
| 排名 | Rank IC、precision@K、NDCG | 换手、板块/规模暴露 |
| 提醒 | episode 命中率、净方向收益 | MFE/MAE、延迟、重复率、覆盖率 |
| 分析师 | residual return、Brier/校准、日期簇 IC | 羊群相关性、延迟、修订稳定性 |
| 组合 | 净收益、回撤、Calmar/DSR | 容量、换手、行业集中、non-fill |
| 运行 | scan SLO、投递成功率 | 线程池/DB池水位、SSD、outbox backlog |

## 6. 外部项目与书籍：吸收什么，不吸收什么

| 参考 | 借鉴 | 明确不做 |
|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | PIT 数据适配、Alpha158 基线、IC/组合实验、Recorder | 不替换实时服务，不把示例模型直连飞书 |
| [Qlib Recorder](https://github.com/microsoft/qlib/blob/main/docs/component/recorder.rst) | 参数、数据版本、随机种子、预测、组合结果归档 | 不再允许不可复现实验 |
| [QuantConnect LEAN Algorithm Framework](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview) | Alpha -> Portfolio -> Risk -> Execution 分层 | 不整体迁移到 C#，不照搬美股现实模型 |
| [NautilusTrader backtesting](https://nautilustrader.io/docs/latest/concepts/backtesting/) | 单调事件时钟、订单状态、部分成交、流动性消费 | 五档快照不冒充 L2/L3 队列 |
| [vectorbt](https://vectorbt.dev/) | 大范围参数敏感性与稳健性曲面初筛 | 不用最高 Sharpe 直接晋级 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | train/validation/test/paper 分离、受约束环境 | 不用每日少量盈亏在线训练 DRL |
| [River](https://jmlr.org/papers/v22/20-1380.html) | progressive evaluation 与漂移检测思想 | 漂移报警不自动重训上线 |
| [FinBERT](https://arxiv.org/abs/1908.10063) / [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 金融语言任务基准、结构化抽取评估 | 情绪模型分数不直接下交易结论 |

方法与团队共同读物：

- [Advances in Financial Machine Learning](https://uat.store.wiley.com/en-us/advances-in-financial-machine-learning-p-9781119482109)：标签重叠、purging/embargo、meta-labeling 与研究纪律。
- [Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) 和 [Probability of Backtest Overfitting](https://scholarworks.wmich.edu/math_pubs/42/)：约束反复试验和挑赢家。
- [Tracking the Best Expert](https://mwarmuth.bitbucket.io/pubs/J39.pdf)：分析师 Fixed-Share 的理论依据。
- [The Price Impact of Order Book Events](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1712822)：OFI 应做窗口聚合和深度归一化，不能用单帧方向当稳定 alpha。
- [Do Industries Explain Momentum?](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00146)：板块相对强度与个股联动应作为独立研究问题。
- [Active Portfolio Management](https://www.mheducation.com/highered/mhp/product/active-portfolio-management-quantitative-approach-producing-superior-returns-selecting-superior-returns-controlling-risk.html)：IC、breadth、风险预算与组合构造。
- [Algorithmic Trading](https://uat.store.wiley.com/en-us/algorithmic-trading-winning-strategies-and-their-rationale-p-9781118746912)：回测陷阱、状态变化与策略实施。
- [Trading and Exchanges](https://academic.oup.com/book/52292)：订单、流动性、价格时间优先和交易成本的共同语言。

总体原则不是引入七套框架，而是：Qlib/vectorbt 负责发现，LEAN/Nautilus 提供现实语义，AFML 负责证伪，Fixed-Share/River 负责保守演化，LLM 只负责可审计抽取。

## 7. 分阶段实施计划

### Phase 0：止血与唯一真相（0–3 个工作日）

| ID | 任务 | 代码落点 | 验收 |
|---|---|---|---|
| P0-S1 | 统一复权研究价 | `main.py:970`、`factor_lab.py:40`、`post_close_structures.py` | 合成除权夹具与本地完整样本连续；缺因子跨日特征不参与确认，日内原价路径不受影响 |
| P0-S2 | 实时市场/数据 `policy_gate` | `main.py:4123,4981`、market state | risk-off 禁 entry；stale/停牌不确认；无持仓证据的 hard stop 只作风险告警 |
| P0-S3 | 盘后同日语义 | `main.py:3139,5582`、`strategy_read_model.py` | T 不完整/T-1 完整时绝不标 T 完成；补齐后自动重跑 |
| P0-A1 | 拆分析师同步并差量限速 | `build-remote-archive-sync-workflow.mjs`、新 cursor migration | 连续 10 轮 0 个 429；报告故障不阻断消息 |
| P0-A2 | 修上海日与历史 as-of | `analyst_expert_research.py` | 00:01 北京边界正确；8/20 快照看不到 8/21 才成熟结果 |
| P0-A3 | 唯一 promotion registry | 推荐、scorecard、expert research | 未人工批准前 analyst 权重强制为 0；故障自动归零 |

本阶段不新增技术指标、不拉历史数据。

### Phase 1：统一契约与 episode（第 1 周）

1. 新建 `app/strategy_contracts.py`：`SignalSpec`、`EvidenceRef`、`PolicyDecision`、`LabelSpec`。
2. 从 `main.py` 机械迁出 signal rules、policy gate、episode state；实时和未来回放共用纯函数。
3. 增加 `signal_episode_id`、material state、clear/rearm；静态极值 30 分钟最多提醒一次，阶段升级可穿透。
4. 新建 append-only `analyst_observations` 与 `analyst_extraction_runs`；日报和消息进入同一 corpus。
5. 自动候选池增加 `discovered_at / expires_at / reason_codes / source_snapshot`；Top20 仅前端，人工提升才进 watchlist。
6. 前端新增“候选漏斗、episode 时间线、数据缺口、分析师同步健康”四张卡。

验收：任一提醒能反查全部 evidence、时间、规则版本和 episode；同一输入在实时与纯函数测试中结果一致。

### Phase 2：前向结算与纸面组合（第 2 周）

在不拉历史数据的前提下先用每天新增数据前向积累：

1. fixed horizon 之外增加 triple-barrier outcome；保留 barrier trial 版本。
2. 分析师消息增加 5/15/30/60m、午收、收盘和日频两个独立账本，缺报价明确 `unavailable`。
3. 建立 paper order/position/portfolio/risk ledgers。
4. 加入 A 股 T+1、整手、各市场涨跌停、停牌、费用、滑点、延迟和 non-fill 模型。
5. 日终生成一份统一摘要：提醒 episode、纸面决策、净成本结果、盘后候选、分析师新观点、数据质量和门禁。
6. 前端新增纸面组合净值、暴露、可卖量、风险事件和“市场-only / 联合模型”消融视图。

验收：同一 knowledge cutoff 可重复得到字节级相同决策；风险模型只削减风险，不能反向创造 entry。

### Phase 3：分析师 Prompt Lab 与联合 shadow（第 3–4 周）

1. 建立人工金标集：标的、动作、方向、周期、条件、否定、止损、风险、证据 span。
2. `strict_action`、`scenario_context`、`risk_first` 真正各自产生版本化候选，不再只统计 coverage。
3. 每位分析师使用共享 schema + 个性 adapter；禁止复制独立词表形成五套不可比逻辑。
4. 按固定时间切分评估 extraction precision/recall、风险遗漏和下游残差收益。
5. Fixed-Share 升级为分层 expert，始终向等权收缩并扣除羊群相关性。
6. 联合模型只跑 shadow：同一事件并排生成 market-only 与 market+analyst，不改变飞书内容。
7. champion/challenger 的晋级和回滚必须有人审、可审计、可一键归零。

验收：每个 prompt 输出都能回到原文 hash 和 span；至少 200 个成熟动作、60 个交易日且样本外显著之前，状态保持 `collecting`。

### Phase 4：历史回放与严格验证（等待用户另行授权）

本阶段当前明确暂停，不自动触发 provider，也不下载历史。得到授权后再执行：

1. 以离线导入或受控回填建立无偏日线、分钟、停牌、涨跌停、复权和退市股票池。
2. 同一 `SignalSpec` 走事件时钟回放；禁止另写一套“回测版规则”。
3. vectorbt 做参数稳健性初筛；Qlib Alpha158/线性/LightGBM 做日频基线。
4. purged walk-forward + embargo；样本 uniqueness；成本压力；DSR/PBO；多随机种子。
5. 至少 60 aligned days、200 independent signals、每 cohort 30 条；分析师仍遵守自身更严格门禁。

验收：所有未通过项目仍为 `descriptive_only`；回放结果不能自动写 live 配置。

### Phase 5：纸面晋级与漂移治理（样本门禁后）

晋级状态机：

```text
draft -> shadow -> reviewable -> paper -> advisory_champion
                     |             |
                     +-> rejected  +-> rollback
```

- shadow 至少 20 个交易日；paper 再至少 20 个交易日；均需人工批准。
- progressive evaluation 监控规则净收益、触发频率、分析师校准、特征缺失和源延迟。
- warning 只能降权/启 challenger；drift 必须冻结晋级、归零分析师 delta 并要求复核。
- 不存在自动下单状态；`advisory_champion` 仍只产出决策卡和纸面目标。

### Phase 6：RL 沙盒（长期可选）

只有 Phase 0–5 全部通过后，FinRL 风格环境才可研究，并限于：候选排序、提醒预算、target-risk overlay。RL 不得改 primary setup、不得绕过风险上限、不得在强选择偏差观察池上训练；必须击败等权、规则分数、线性/LightGBM 和 Fixed-Share，并通过多种子、样本外、成本压力与纸面运行。

## 8. 前端目标

前端不再只展示“结果表”，而展示完整决策漏斗：

1. **市场状态**：指数、板块分钟资金增量、轮动事件、数据新鲜度。
2. **候选漏斗**：全市场 -> 板块 Top20 -> 自动候选 -> 人工观察 -> confirmed episode -> paper proposal。
3. **策略卡**：版本、当前状态、触发数、独立 episode、门禁、近期净结果、漂移。
4. **分析师矩阵**：同步健康、消息/日报数、eligible/matured、技能维度、羊群相关、权重为何为零。
5. **证据时间线**：市场、板块、个股、盘口、分析师在同一上海时间轴上，明确 available_at。
6. **纸面组合**：净值、持仓、可卖量、板块/策略暴露、风险事件和成本。
7. **实验治理**：trial 数、champion/challenger、DSR/PBO、批准/回滚历史。

飞书继续只发显式观察池中通过策略门禁的事件；自动挖掘和日终研究默认只更新前端，避免噪声。

## 9. 存储、内存与清理预算

当前磁盘尚有约 208 GiB 可用，但应按用户给出的研究空间约束做有界设计，而不是因为空间充足就无限保存。

建议为量化新增数据设 20 GiB 软预算：

| 类别 | 预算 | 保留策略 |
|---|---:|---|
| PostgreSQL 热数据 | 8 GiB | 月/日分区，超过水位先聚合再清理 |
| 压缩研究 artifact / Parquet | 5 GiB | 按 trial/version，可复现优先 |
| 备份 | 5 GiB | 每日增量/定期全量，按保留策略轮转 |
| 安全余量 | 2 GiB | 达 80% 告警，达 90% 停非必要采集 |

已有保留边界继续执行：盘口和 rt_k 高频证据 7 日、分钟同刻剖面 90 日、板块分钟曲线/轮动 60 日；清理前先保留 1m/5m 聚合、episode 特征和 outcome，原始高频不永久留库。canonical 日线、策略/观点版本、trial、批准记录和结算结果长期保留。

Qlib/vectorbt/LLM 评估必须在独立离线 worker 中串行或小并发运行，不导入 `quant-research` 实时进程。实时容器维持当前约 224 MiB 量级；离线 worker 设置明确内存/CPU/磁盘限额，完成后删除临时数组和中间文件，只保留 manifest 与必要 artifact。

## 10. 测试与运行 SLO

### 必须新增的测试

- PostgreSQL 真实事务：复权研究视图、不可变 received_at、message version、CN 日期边界、as-of 无未来。
- 事件纯函数：实时/回放一致、episode clear/rearm、market/risk/T+1 gate。
- n8n 集成：429、`Retry-After`、分页、100+ 突发、同时间戳、报告失败不阻断消息。
- 抽取金标：否定、条件、简称映射、目标/止损、风险遗漏。
- 纸面撮合：涨停不可买、跌停不可卖、停牌、100 股、卖出税、最低佣金、部分成交和 non-fill。
- 前端：blocked/latest completed 并列、门禁为零、同步错误可见、候选不会误入观察池。

### 建议 SLO

- 观察池行情与策略评估 p95 小于当期扫描间隔；关键输入过期时绝不 confirmed。
- 特别窗口 10 秒扫描只覆盖观察池批量报价；慢全 A 横截面不得阻塞观察池路径。
- 飞书投递成功率 >=99%，失败进入 durable outbox；不得因冷却丢失未送达 episode。
- 分析师同步连续 10 轮无 429；新消息正常情况下 15 分钟内本地可见。
- 100% 决策对象含 knowledge cutoff、数据版本、策略版本、reason codes 和 evidence refs。
- 20 GiB 软预算 80% 告警、90% 自动停止非必要高频原始采集。

## 11. 推荐的第一批提交顺序

1. `fix: split and rate-limit analyst message synchronization`
2. `fix: enforce analyst Shanghai PIT and as-of boundaries`
3. `fix: unify analyst promotion gate and keep live weight zero`
4. `fix: unify adjusted research price contract`
5. `fix: require same-date post-close completion`
6. `feat: add live market and data policy gate`
7. `feat: add signal episode lifecycle`
8. `feat: add append-only analyst observations and extraction runs`
9. `feat: add paper portfolio, T+1 and portfolio risk models`
10. `feat: add market-only versus analyst ablation dashboard`

每个提交都应小而可回滚；完成一项就跑直接单测、真实 SQL 测试、服务健康检查和对应前端 build。不得把 P0 修复与新策略阈值混在同一提交。

## 12. 完成定义

这个计划的“完成”不是策略数量增加，而是达到以下状态：

- 同一规则在实时、回放和纸面组合中使用同一契约和事件时间语义；
- 任一提醒、观点和纸面决策都能重建当时知识边界；
- 分析师同步稳定，消息和日报互不拖累，媒体边界不变；
- 市场-only 基线始终存在，分析师只能证明增量价值后进入有限 prior；
- 结果包含 A 股可交易性和净成本，不把观察价格当成交价格；
- 自动演化只生成 challenger，不直接改 champion；
- 数据不足时系统清楚地说“不知道”，而不是用更复杂的模型掩盖样本不足。

## 13. 2026-08-13 执行记录（不含历史回填）

本轮在不拉取历史行情、不连接券商的前提下完成了第一批可运行闭环：

- 新增 `strategy_contracts.py`，统一 `SignalSpec`、`EvidenceRef`、`PolicyDecision` 和 `LabelSpec` 的可序列化契约，供实时、纸面和未来回放共用。
- 新增版本化 Alembic 迁移 `20260814_0020_paper_research_ledger`：策略试验/契约、纸面决策、纸面订单、持仓、组合快照和风险事件均为 append-only/可审计实体。
- 已确认的盘中信号只生成 `paper_decisions` 研究提案；明确写入 `paper_only`、`manual_review_required`，没有任何 broker client 或真实委托路径。
- 新增 `intraday_signal_episodes` 生命周期：按策略族/方向/分钟级物化状态复用 episode，清除后才允许 rearm；每个 signal event 保存 episode、阶段和物化状态哈希。
- 新增统一 `analyst_extraction_runs` / `analyst_observations` 事实链；报告与消息均记录 PIT 时间、来源版本、内容哈希和抽取版本，实时上下文只读取 `eligible`，晋级注册表默认 disabled/权重 0。
- 新增预注册 triple-barrier 纸面标签、组合快照与 fail-closed 风险门禁；快照按分钟桶合并，watch 不产生虚拟仓位，所有结果保持 paper-only。
- 前端已展示策略漏斗、episode、纸面账本、分析师观察、同步游标、晋级和策略合约状态；新增只读治理接口。
- 新增 A 股纸面约束：100 股整手、T+1 可卖量、停牌、涨停买入/跌停卖出 non-fill 风险、最低佣金、卖出印花税、滑点和 triple-barrier 纯函数标签。
- 新增只读接口 `/api/v1/paper/status`、`/api/v1/strategy/contracts`，并通过 Feishu adapter 映射到前端；前端新增“纸面策略账本”卡片。
- 分析师同步已拆成报告流与消息流两个独立工作流；旧合并流停用。报告和消息不再互相拖累，部署脚本会保留前后快照并校验发布版本。新消息仍只取已提取文字，不跟随媒体 URL。
- 分析师研究的上海日期和 `exit_date<=as_of_date` 已统一；`recompute_scorecards` 已在真实本地数据库调用通过，未来成熟结果不会污染历史快照。
- 新增 `20260814_0027` 纸面账户/成交回执账本：手动接受才会模拟成交，报价必须来自本地已落库 Tencent/Super GET 证据；账户、费用、滑点、持仓和 T+1 日界切换均可追溯。
- 新增 `20260814_0028` Prompt Lab 与分析师盘中结果账本：三种确定性 challenger 变体、人工金标、离线 precision/方向准确率、5/15/30/60 分钟结果均为 append-only；没有人工金标或样本外门禁时状态只能 collecting/insufficient_labels，实时影响恒为 none。
- 前端新增 Prompt Lab、纸面账户/订单状态和门禁显示；Feishu adapter 增加纸面账户、动态接受、Prompt Lab 标注/评估的安全代理，所有写请求仍要求 `X-Quant-Write-Key`。
- 新增 `20260814_0029` 策略消融账本：并行记录 market-only、固定 10% analyst-shadow 和实际 applied 分数；shadow 只用于离线比较，promotion registry 未批准前实际分析师权重仍为 0。
- 盘口研究证据扩展到 30 秒/1 分钟/5 分钟封单侵蚀、Kyle λ/VPIN/CORD 代理，全部标记为未校准 research-only，不进入实时阈值。
- 报告/消息同步工作流已拆分；报告差量游标在一次运行中使用完整版本快照，避免分页造成重复详情请求；两条流独立限速、可独立告警。
- 盘后自动候选增加 `discovered_at`、`expires_at`、`reason_codes` 与 `source_snapshot`；读取模型和前端同时展示有效期、理由和当次数据覆盖。候选仍只做前端研究，不会自动入观察池。
- n8n 主进程和外置 runner 都显式注入分析师同步必需的非持久化运行环境；旧合并流仍停用，报告流与消息流保持独立。健康页同时读取工作流 active/published、最近执行状态和本地游标；当前最近执行仍为旧错误，消息流为 `never_succeeded`。下一交易时段仍须完成连续 10 轮的实际同步验收，才可关闭 429/TLS 风险项。
- 2026-08-13 盘后实测：同日盘后候选任务完成且返回 0（严格门槛，不回退到旧交易日）；涨停池两源去重并集 85、连板梯队 24、分钟形态样本 20、精选 10，均已在研究台展示。
- 2026-08-13 盘后同步修复：远端 `/analysts/{analyst_id}/messages` 真实 Bearer 请求对安强返回 2 条、其余分析师返回合法空集；n8n 失败执行的实际 URI 为 `/analysts/undefined/messages`，根因为游标返回 `remote_analyst_id` 而工作流使用 `$json.analyst_id`。已恢复原 15 分钟交易时段调度并重新发布两条独立流；当前等待下一正式调度验证本地游标推进。
- 2026-08-13 后续同步加固：远端 `/messages/updates` 增量接口已用真实 Bearer 请求验证，返回安强 2 条消息与 `next_cursor=null`；新增 `analyst_global_sync_cursors` 迁移和有歧义路径避让，正式消息流已确认无 pagination 配置、单页上限 100，详情全部导入后才写回游标。
- 新增只读 `/api/v1/strategy/health` 与前端“策略健康与漂移”卡：展示 7 日触发频率、独立 episode、30 分钟成熟结果、报价新鲜度及 200 信号/60 交易日门禁。它只做研究监控，明确 `live_effect=none`，不会在线调参、晋级策略或改变分析师权重。
- 新增研究存储水位治理：健康接口和前端显示量化热库/受管研究空间用量；默认热库 8 GiB、总研究空间 20 GiB，达到 80% 仅告警，达到 90% 暂停盘口、1 秒 `rt_k`、分钟档案和板块曲线等非必要高频原始证据。观察池报价、风险提醒、outbox 与结算不受影响，也不自动删除证据。当前实测热库约 1.32 GiB（16.5%），总受管空间约 1.32 GiB（6.6%），状态 healthy。
- 验收：quant-service 全量 267 项 Python 测试通过，前端 typecheck/Vite build 通过；健康接口为 `ok`，迁移为 `20260815_0030`，Prompt Lab 当前无候选且 live_effect=none（符合历史不回填和数据门禁）。

仍明确未完成或待运行窗口验收：分析师消息流真实连续 10 轮无 429、历史数据回填（按要求暂停）、分钟回放、60 日/200 信号验证、样本外分析师 champion/challenger 晋级和 RL。纸面成交撮合仅支持“已有本地报价证据 + 人工确认”的研究模拟，不是经纪商成交。上述项目继续保持 `research_only`，不会改变实时规则或阈值。

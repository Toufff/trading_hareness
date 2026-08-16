# 观察池实时策略增强计划

日期：2026-08-16（Asia/Shanghai）  
状态：`research_only`；不自动下单；历史数据拉取与历史分钟回放暂停，待单独授权。

## 结论

不引入完整 Qlib、LEAN 或 vn.py 引擎。保留现有 Python + PostgreSQL + 证据链 + 飞书 outbox，移植其已验证的架构模式：Qlib 的数据/因子版本契约，LEAN 的预测、组合、风险职责分离，Nautilus 的双时钟不可变事件，vn.py 的录制与前置风控。每条提醒继续是人工复核线索，而不是交易指令。

当前不改变 live 阈值：现有样本时间跨度、复权覆盖和历史分钟回放均不足以合法校准。新逻辑先以 `shadow_only` 或 `watch` 记录证据与结算，再按独立交易日门禁晋级。

## 当前实现审计与立即优先级

截至 2026-08-16，事件、`FactorSpec`、`SignalSpec`、episode、纸面组合和飞书 outbox
已具备可追溯的基础；它们不等于策略已经被验证。以下问题必须先于任何新 entry
规则收口：

1. **研究价格口径统一**：盘中推荐特征、盘后结构和因子实验必须共用带版本的
   adjusted research view；撮合、涨跌停和成交可行性继续使用原始价格。缺少复权因子
   时必须写质量标记，不能静默混入同一序列。
2. **门禁前置**：市场风险、行情新鲜度、跨源冲突、可卖数量、涨跌停和组合约束必须
   在 `confirmed` 以前决定 `block/watch/risk_alert_only`；它们不能只作为事后归因文字。
3. **盘后日期语义**：某交易日的盘后任务只可在该日完整横截面准备好时标为 completed；
   不能因上一完整交易日可用就把当天误标成功。读模型同时展示 latest attempt 与 latest
   completed，避免 blocked run 掩盖可用旧结果。
4. **共享快照与 episode**：全 A 横截面只按 30--60 秒共享更新；观察池报价走批量高频
   通道。静态极值只提示一次，只有 clear/rearm 或实质指标升级才新建 episode。
5. **微观结构如实降级**：现有五档快照和分钟资金流是订单流代理，未具逐事件撤单与
   成交方向前不得命名为真 OFI。它们当前只可作为归因、watch 或风险降级证据，不给
   entry 阈值加分。

这五项是 P0/P1 的实现顺序；历史拉取、历史分钟回放、阈值再拟合和任何在线学习仍暂停。

## 目标状态机

每只观察标的在每个连续竞价窗口只可处于一类状态：

```text
data_blocked -> observe -> setup -> confirming -> entry_watch
                                     |                 |
                                     v                 v
                                invalidated <- hold / reduce / exit_review
```

四类可解释策略状态：

1. **延续入场**：个股相对市场和精确板块同向走强、站稳 VWAP、同刻放量、广度改善；
2. **超跌反弹**：个股残差极弱但市场/板块未同步恶化，卖压衰竭后重新站回 VWAP；
3. **减仓/离场复核**：价格、订单流代理、板块广度或 VWAP 出现至少两项失效；
4. **禁入**：行情陈旧、跨源冲突、停牌/涨跌停、集合竞价/午休、T+1 不可卖、板块成员覆盖不足或组合风险门禁触发。

“订单流”严格分级：有逐事件委托、成交和撤单时才称 OFI；当前五档快照/分钟资金流只能称为 **订单流代理**，不得假称真 OFI。

## 数据源分工

| 用途 | 主数据 | 备用/验证 | 可进入实时决策的条件 |
|---|---|---|---|
| 观察池价格、量比、换手、资金代理 | 腾讯批量行情 | 新浪价格 | 必须记录源时间与最大年龄；跨源冲突降级 |
| 秒级确认 | Super GET `rt_k` | 腾讯 | 仅作新鲜的交叉确认；stale/missing 不得确认 entry |
| 分钟路径、VWAP、同刻量能 | Super GET `rt_min` | 已落库分钟观察 | 轮转覆盖、累计量额单调且同一连续竞价段 |
| 板块资金与轮动 | 东财盘中资金流 | 本地增量曲线 | 每一分钟快照记录 coverage、available_at；只按同一 taxonomy 解释 |
| 精确板块成员与盘后 Top10 | Tushare `moneyflow_cnt_ths` + `ths_member` | — | 只接受点时精确成员，禁止中文名称猜测 |
| 龙虎榜、涨停/连板关系 | Tushare/东财盘后数据 | 本地关联挖掘 | 作为次日研究先验，不伪装成盘中订单流 |
| 分析师文字 | 远端已提取文本 | — | 仅 `received_at` 是策略时间；`stated_at` 仅用于安强动作复盘 |

## 实施顺序

### P0：实时证据与风控契约（优先）

1. 建立不可变 `MarketEvent`：`event_time`、`available_at`、`ingested_at`、`source_sequence`、`payload_hash`、`quality_flags`；回放按 `available_at, source_sequence, event_id` 排序。
2. 建立 `FactorSpec` 注册表：因子版本、输入、预热期、频率、时点语义、缺失规则、是否可用于 live；研究价与撮合原价严格分离。
3. 将 `policy_gate` 置于“规则命中”与“confirmed”之间；市场风险、数据新鲜度、可交易性、T+1 可卖量、涨跌停、纸面组合暴露只能阻断/降级，不得以缺数据放行。
4. 将观察池覆盖从静默截断变成显式矩阵：每轮显示 `covered / stale / missing / deferred`，分钟和盘口轮转按实际池子长度计算。
5. 飞书和前端的每个信号固定展示：观测时间、推进因素、阻止/降级因素、失效条件、数据新鲜度、策略版本；概率样本不足时明确显示“历史条件基准率”，不称预测概率。

验收：相同事件输入产生相同 signal hash；任何 stale/missing 价格不产生 confirmed entry；午休/跨夜结果保持 `unavailable`，不借用下一时段报价。

### P1：观察池四类策略（只影子记录）

1. **开盘区间突破与回踩承接**：开盘区间突破后，不追第一脉冲；要求回踩不破 VWAP/区间上沿、3 分钟残差收益转正、同刻量能与板块广度确认。
2. **趋势回踩延续**：价格在 VWAP 上方，1/3/5 分钟收益、量能和相对板块收益同向；把高位缩量回踩与失败跌破分开。
3. **B 浪/超跌反弹**：保留当前日线状态 + 分钟接受确认；通过精确、点时板块成员自动生成 peers。入场、持有、减仓和退出使用同一状态机，T+1 仅允许风险告警而非伪造可执行卖出。
4. **板块轮动与龙头—滞后股**：用板块相对市场收益、站上 VWAP 的成员比例、流入连续性、龙头不含目标股的收益作为条件；必须滚动样本外验证，失败时仅保留“广度确认”，不能作为固定领先规则。
5. **失败突破/资金背离离场**：价格创新高但订单流代理、广度、VWAP 中任两项转弱，产生 `reduce`/`exit_review`，而非自动卖出。

验收：每个 entry 都有机器可判定 invalidation；每一类策略均可输出“不推进”的原因；新策略只写 evidence、episode 和 outcome，不进入 live 加分。

### P2：确定性回放与 A 股现实模型（回放暂停，需历史数据授权）

1. 用已录制的行情事件建立虚拟交易时钟，不在回放时访问供应商；live 与 replay 调用同一策略纯函数。离线分钟文件必须分别保留 `bar_time`、供应商的 `source_available_at` 和本地导入 `available_at`：只能按前者的来源可用时间回放，缺失时钟不得用 K 线结束时间或导入时间替代。当前 `offline_minute_bar` 适配器只构造确定性事件；未具备同刻报价、板块、日频特征与观察池快照的分钟文件不得冒充能够重跑 live 规则。自本次上线起，实时扫描另以 `intraday-rule-input-v1` 冻结每个观察标的（含无信号行）的最小规则输入并按 60–120 天有界留存；这只为未来真实扫描的可复现性采证，不补抓、也不回写过去。
2. `AshareRealityModel` 基础契约已收敛至 `app/ashare_reality.py`：交易日历/午休继续使用既有时段规则，且 T+1、100 股整手、停牌、不同板块/ST 涨跌停、费用、滑点、不可成交已由实时风险与纸面成交共用；只缺获授权历史路径上的回放验证。
3. 建立黄金交易日：信号、抑制、风险原因码、策略/概率版本均哈希比对。
4. 用 point-in-time 观察池和板块成员消除选择偏差；不使用当天最终 VWAP、修订成分、收盘资金流或未来复权信息。

门禁：每策略至少 60 个独立交易日、200 个独立成熟事件、每个市场状态 cohort 至少 30 个事件；在此之前所有新策略保持 `descriptive_only`。

### P3：条件概率与研究纪律（P2 后）

定义具体目标，禁止把分数线性换成概率：

```text
entry: P(未来 15 个交易分钟先到达目标收益，而非先触发止损 | 当时状态)
exit:  P(未来 H 分钟先触发风险失效 | 当前持仓和可交易性)
```

按完整交易日做 purged walk-forward，标签跨午休按交易分钟处理；仅用 OOF 预测做 Beta/Sigmoid 校准。样本池逐级回退：`setup+regime+time_bucket -> family+regime -> family -> global prior`。报告 Brier、LogLoss、ECE、可靠性图、样本数、独立日期和保守区间；所有试验登记并用 SPA/Romano-Wolf 控制多重检验。

### P4：纸面组合、分析师和持续监测（P3 后）

1. 按 `Insight -> PortfolioTarget -> RiskAdjustment -> Alert` 分层；概率只改变风险预算上限，不能越过硬门禁。
2. 组合层增加单票/板块/相关簇、流动性冲击、换手、日亏、回撤和新开仓预算。
3. 分析师默认权重为零；仅在统一 promotion registry 人工批准后，作为 5–10% 上限的 context prior。远端同步失败、抽取漂移或无成熟结果时自动归零。
4. 生产只前向影子：研究 -> 回放 -> shadow -> 仅提醒 -> 纸面组合；不自动下单。

## 前端与飞书

前端新增/固定四个面板：

- **实时状态机**：每标的状态、最后事件、覆盖与阻断原因；
- **策略证据**：分钟路径、VWAP、残差、板块广度、资金变化、五档代理；
- **概率健康**：只展示已校准模型；否则显示样本不足与门禁进度；
- **研究台**：shadow 候选、板块/龙虎榜/分析师上下文、episode 与结算。

飞书只向显式观察池发送 confirmed 的 entry/reduce/exit_review，消息必须包含“为什么推进、为什么没有完全确认、何时失效、数据是否新鲜”。自动挖掘和涨停关联候选只进前端批量研究台。

## 不采用的做法

- 不把 VPIN 当方向或胜率信号；最多作为 P2 后的实验性流动性压力 veto。
- 不在无逐事件盘口/撤单时把分钟资金流称为 OFI。
- 不使用 LSTM/Transformer、线上 RL 或线上自动调参；先比较可解释基线和样本外校准。
- 不把行业 lead-lag 固化为规则；只在本地时点一致样本中滚动验证。
- 不以当前人工观察池回放结果宣称无选择偏差。

## 研究依据到本系统的落点

| 依据 | 可以借鉴的部分 | 本系统的边界 |
| --- | --- | --- |
| Qlib Data Handler | 数据、处理器和因子定义版本化，训练与推理使用同一已登记输入 | 只导出通过 PIT/覆盖率门禁的本地快照；不因安装 Qlib 而自动下载历史数据 |
| LEAN Algorithm Framework | `Insight -> target -> risk -> execution` 职责分离 | 当前只到 `Insight -> risk gate -> alert`；无券商执行或自动下单路径 |
| NautilusTrader 双时钟 | 同时保留外部事件时间与本地接收/初始化时间，回放按可得时间稳定排序 | 对报价和分析师文本分别保留 `event/observed_at` 与 `available/ingested_at`；分析师实时决策只用 `received_at` |
| Cont--Kukanov--Stoikov | 真正 OFI 包含限价单、市价单、撤单等逐事件数据，且影响受盘口深度制约 | 当前仅有五档快照，故 QI/差分只记为订单流代理，不参与 entry 加分 |
| 概率校准与 DSR | 概率必须用样本外预测校准并审计 Brier/LogLoss；大量规则试验需要处理选择偏差 | P2 前没有任何“买入成功概率”；所有阈值保持既有值，策略仅输出证据与 `descriptive_only` 结论 |

因此，后续“RL/evolve”只能是离线、冻结样本上的 challenger 研究：先固定标签与时间切分，再比较候选策略，最后人工批准。它不会在盘中用刚发生的盈亏自动改写当前规则。

## 参考实现与文献

- Qlib data/handler 的因子、处理和数据健康分层：https://qlib.readthedocs.io/en/latest/component/data.html
- LEAN 的 Insight 生命周期与风险、组合、执行分层：https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/alpha/key-concepts
- NautilusTrader 的 event 与 init 双时钟：https://nautilustrader.io/docs/latest/concepts/data/
- scikit-learn 的概率校准与小样本限制：https://scikit-learn.org/stable/modules/calibration.html
- Cont、Kukanov、Stoikov 的订单簿事件与 OFI 原始研究：https://arxiv.org/abs/1011.6402
- Bailey、López de Prado 的 Deflated Sharpe Ratio（多重试验/选择偏差）：https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- 中国市场日内动量/反转研究：https://www.sciencedirect.com/science/article/abs/pii/S1544612318307414
- 订单不平衡对中国股票收益的研究：https://www.sciencedirect.com/science/article/pii/S0927538X15300056

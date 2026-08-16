# 实时策略强化计划：状态机、证据与概率

更新：2026-08-16（Asia/Shanghai）
状态：研究与提醒系统；**不自动下单、不拉取新的历史数据**。

## 1. 已核验的当前边界

- 盘中扫描：连续竞价普通窗口 30 秒；重点窗口 10 秒；Super GET 只在重点窗口以 1 秒节奏作价格交叉确认。普通窗口的 `rt_min` 每轮最多验证 4 只，游标以实际观察池长度闭环推进，不以 40 只容量上限推进而漏掉尾部标的。
- 观察池：36 只启用标的，腾讯五档盘口一次批量请求覆盖上限 40 只；超过上限应显式拒绝/分页，不能静默遗漏。
- 存储：研究总预算 40 GiB、热数据库预算 28 GiB；2026-08-16 实测约 12.4 GiB，采集治理为 `healthy`，非必要高频证据没有被暂停。
- 板块：东财板块资金曲线按一分钟持久化；跨源成员关系只使用精确、同源映射，不能按中文名称猜测 join。
- 分析师：远端只同步已经提取好的文字；`received_at` 是 live 策略唯一可用时钟。`stated_at` 可做作者时点复盘，但永久是 `replay_only`。

## 2. 本轮已完成的证据止血

### 2.1 同连续竞价段结算

固定期限收益的 5/15/30 分钟出口只允许使用目标时刻后 90 秒内、同一段连续竞价的已存腾讯报价。午休、收盘和隔夜不会借用下一时段第一笔报价。

重要细节：盘后重算仍会查询**原始有界窗口**。超过 90 秒只说明实时等待结束，并不代表数据库里那段窗口的有效报价应被忽略。2026-08-16 重算后，已成熟结果最大额外延迟为：5m 61.47 秒、15m 82.08 秒、30m 29.91 秒；均在容忍范围内。

### 2.2 分析师双时钟账本

- `received-at-local-quote-session-bounded-v2`：用于 analyst observation；只从 `strategy_available_at` 起算，不给延迟归档的文本虚构实时优势。
- `author-stated-local-quote-session-bounded-replay-v1`：只用于安强动作复盘；从 `stated_at` 起算，记录 `replay_only=true` 和 `strategy_effect=none`。

当前安强非中性动作 84 条中，每个期限只有 3 条具备完整本地行情窗口；其余 81 条明确 `unavailable`。因此不能据当前结果评价其“胜率”，更不能进入分析师权重。

### 2.3 Prompt Lab 的真实门禁

候选以不可变 `observation × variant × version` 保存，当前已物化 3,771 条 observation、11,313 个候选。候选生成先把 PostgreSQL `numeric` 显式转成 JSON 数值，避免整批回滚。

人工金标评估按 `strategy_available_at` 的上海交易日留出最后 20%，绝不随机打散。`completed` 至少要求：30 个金标、训练/留出均非空、留出不少于 10 条；此门槛只让离线报告可供人工复核，仍不能改变 live 规则。当前三个变体均为 `collecting`。

## 3. 目标策略不是“堆指标”，而是状态机

每个观察池标的在一个时点只处于一个主要状态，所有状态必须说明证据与反证：

| 状态 | 可观察的推进证据 | 失效/降级 | 当前动作 |
| --- | --- | --- | --- |
| 延续入场候选 | 个股相对市场/板块同向、守住 VWAP、量能与板块广度同步改善 | 跌回 VWAP、板块广度转弱、报价过期 | 研究提醒；由风险层决定是否给纸面建议 |
| 超跌反弹候选 | 残差跌幅极端、卖压衰竭/转正、重新站回 VWAP、板块未同步恶化 | 低点再破、流入转流出、波动压力上升 | 研究提醒，先观察再确认 |
| 减仓/离场候选 | 上涨但订单流/广度背离、持续失守 VWAP、龙头先弱 | T+1 无可卖量、涨跌停/停牌、数据缺失 | 风险告警；不能伪装成可执行卖出 |
| 禁入 | 数据陈旧、盘口覆盖不足、午休/集合竞价、接近涨跌停、流动性枯竭 | 数据恢复且重新满足 setup | 只记证据，不发 entry |

这里的“订单流”必须严格命名：现有五档快照可生成队列不平衡和订单流**代理**；没有逐事件委托、成交方向和撤单数据时，不能称为真实 OFI，也不能把它直接当买入因子。

## 4. 因子优先级与实现顺序

### P1：先补现有实时信号的闭环

1. **B 浪反弹生命周期**：补 `entry -> hold -> reduce -> exit -> cleared`，把 VWAP 失守、3/5 分钟动量转负、资金流转负、板块广度崩塌和日线状态失效写成机器原因码；T+1/可卖量只能由风险层削减或阻断。
2. **点时板块共振**：按 `(taxonomy_key, sector_key, effective_at)` 自动生成 peer，排除目标股自身，返回覆盖率；映射不完整时只用个股证据，不能伪造“板块确认”。
3. **覆盖与新鲜度**：观察池超过经核验的 40 只腾讯批量盘口上限时，扫描现已 fail-closed 并明确写入 `watchlist_capacity`，不会只扫前 40 只却声称全覆盖。确认路径只接受本轮腾讯观察池批量报价，且其交易所时间戳须在重点窗口 20 秒、其他窗口 45 秒 SLO 内；腾讯全 A 快照和新浪补价只保留证据，不能直接触发飞书。Super GET 仅作可选交叉确认，冲突阻断、缺失降级为 watch。轮转按实际观察池长度，而不是写死前 20 只。
4. **episode 而非固定冷却**：同一 setup 只在首次确认、明确清除后重现或实质指标升级时重新提示；静态极值不得每十分钟重复提醒。

### P2：版本化因子与可回放事件

为每个 `FactorSpec` 声明：版本、输入、频率、最小预热、`event_time`、`available_at`、质量门禁、适用（learn/infer）层和 hash。以 `available_at, source_sequence, event_id` 稳定排序；回放只读本地事件，不请求供应商。

已交付第一条可执行链路：`POST /api/v1/strategies/intraday/replay-recorded-events` 只读取
`quant.intraday_signal_events`，按已记录的 `observed_at`（可用时钟）、持久化 source sequence（无该字段时按不可变 event id）回放 episode 生命周期，并把 `input_hash`、`trace_hash`、聚合指标和数据边界写入 `intraday_replay_runs`。相同输入与引擎版本会复用既有 run，避免重复占用存储。它是黄金事件回放和时序审计，不是价格回测、更不是阈值拟合；完整价格/分钟重放仍等待授权数据。

最小可用因子集：

- 个股：1/3/5/15 分钟残差收益、同交易时刻量能 Z 分数、VWAP 距离与斜率、日内高低位、价量背离。
- 板块：相对市场收益、成分股 VWAP 广度、上涨广度、龙头收益与目标股滞后；成员必须点时同源。
- 微观结构：深度归一的队列不平衡、盘口深度、价差、补单/侵蚀速度；OFI 只在后续逐事件数据合格后作为候选。
- 风险：5/30 分钟已实现波动、成交/报价陈旧、涨跌停距离、停牌、T+1 可卖量和组合暴露。

### P3：概率只能是一个明确的事件

不输出泛化的“上涨概率”。入场例子是：

`P(未来 15 个交易分钟先到止盈而非先触发失效 | setup、时段、市场状态、质量门禁)`。

离场另建“未来 H 个交易分钟继续恶化”的目标，不能共享入场模型。校准顺序：按完整交易日 walk-forward 切分 -> 重叠标签 purge/embargo -> 只用 OOF 预测做 Beta/Sigmoid 校准 -> 分层收缩（setup+regime+时段 → 策略族+regime → 全局先验）。

飞书只在门禁通过时展示：目标事件/期限、校准概率及区间、独立交易日/事件数、Brier/LogLoss、推进原因、阻止原因和失效条件。样本不足必须写“暂不可估”，绝不能把线性评分转换成概率。

### P4：组合、提醒和分析师只作独立的上下文层

沿用 `Signal/Insight -> PortfolioTarget -> Risk -> Alert` 的职责分离：

- 信号仅陈述预测，风险层只允许削减或阻断，不能被概率或分析师观点绕过；纸面组合统一应用 A 股 T+1、整手、停牌、涨跌停、成本、单票/板块集中度、日亏损和回撤门禁。
- 每条飞书提醒固定展示“推进证据、阻断/降级证据、机器失效条件、数据新鲜度、事件定义”。概率字段在校准前只能称为“历史条件基准率”，而非“上涨概率”。
- 分析师文本保持独立 evidence 流。消息的 `received_at` 是唯一 live 可用时间；报告的 `first_seen_at` 是报告证据时间；作者在文本中声明的 `stated_at` 只能用在 `replay_only` 复盘账本。任何尚未人工批准的分析师/Prompt Lab 版本权重都必须为零。
- 远端只接收其已抽取的文字，不下载图片、音视频或媒体 URL。报告与消息同步分成独立流、持久游标、限速和 Retry-After 重试；报告流故障不能阻塞消息流。供应商同步故障时，分析师上下文自动归零，而非使用陈旧观点。

### P5：监控、可复现与晋级

- 记录并展示：行情覆盖率、事件延迟/乱序、因子缺失率、信号/episode 状态、风险阻断原因、飞书 outbox 失败、概率校准质量、回放和实时信号哈希差异、纸面组合暴露和数据存储预算。
- 策略健康度按策略族而非“股票:动作:规则”逐行聚合，事件数与去重 episode 数共同展示；这让 trigger drift、样本门禁和回放结果能归因到规则本身，不能被个别高频标的淹没。
- 每个研究运行冻结 `dataset_hash`、因子/策略/标签/校准版本，并保留所有失败试验、窗口与阈值；同一输入事件再次回放必须得到相同信号哈希。
- 晋级顺序固定为 `研究 -> 本地事件回放 -> forward shadow -> 观察池提醒 -> 纸面组合`。每级均可回滚；禁止用新到的一条 outcome 在盘中在线更新 champion。

## 5. 外部研究与项目借鉴

| 来源 | 可迁移模式 | 不照搬的部分 |
| --- | --- | --- |
| [Qlib data layer](https://qlib.readthedocs.io/en/latest/component/data.html) / [Recorder](https://github.com/microsoft/qlib/blob/main/docs/component/recorder.rst) | raw / infer / learn 分层与可复现研究产物 | 不把表达式引擎和在线管理器放进盘中请求链 |
| [LEAN Insight framework](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/alpha/key-concepts) | Signal/Insight → PortfolioTarget → Risk → Alert 的职责分离 | 其默认成交、费用和账户模型不能直接用于 A 股 |
| [vn.py DataRecorder](https://github.com/vnpy/vnpy_datarecorder) / [RiskManager](https://github.com/vnpy/vnpy_riskmanager) | 先录制、可回放、前置风险原因码 | 不复用期货 CTA 的时段与开平仓语义 |
| [NautilusTrader data timestamps](https://nautilustrader.io/docs/latest/concepts/data/) | 区分事件时间和本地接收时间；不可变事件 | 不引入 Rust 高频引擎或假设存在 A 股适配器 |
| [Beta calibration](https://proceedings.mlr.press/v54/kull17a) / [sklearn calibration](https://scikit-learn.org/stable/modules/calibration.html) | 小样本先做保守参数化校准与可靠性评估 | 不在盘中收到单条标签就在线更新模型 |
| [Order book events](https://academic.oup.com/jfec/article-abstract/12/1/47/816163) | 真 OFI 需要事件级盘口、成交和撤单 | 分钟资金流/五档快照不可冒充真 OFI |
| [Industry lead-lag reexamination](https://www.sciencedirect.com/science/article/pii/S0927539815001012) | 板块领先需要本地滚动样本外验证 | 不把 leader→laggard 固化成长期规则 |

VPIN、Kyle lambda、CORD 等仅放在 P2 压力/背离实验池。尤其 VPIN 有明显的研究反证，不能在缺少成交量时间与逐笔数据时充当方向或胜率因子。

### 5.1 盘中状态机的研究约束（新增）

- [A 股日内动量与反转研究](https://www.sciencedirect.com/science/article/abs/pii/S1544612318307414) 指向开盘、午前、午后和尾盘应分别评估：不能把开盘短时反转与尾盘延续混成一个阈值或一条“上涨概率”。
- [订单不平衡对中国股票收益的研究](https://www.sciencedirect.com/science/article/pii/S0927538X15300056) 说明订单不平衡可作为短期候选特征，但仍受内生性、持续性和横截面相关影响。现有五档快照因此仅用于**订单流代理/降级证据**，不是可声称稳定 Alpha 的真实 OFI。
- [Order Book Events 的价格冲击研究](https://academic.oup.com/jfec/article-abstract/12/1/47/816163) 的口径要求真 OFI 包含盘口事件、成交和撤单变化；当前没有逐事件撤单/成交方向，P2 以前不得将五档快照命名为 OFI 或作为 entry 加分。
- [VPIN 的原始流动性压力观点](https://academic.oup.com/rfs/article-abstract/25/5/1457/1569929) 与其 [Flash Crash 反证](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1881731) 相互制约：只把 VPIN 保留为未来的压力 veto 实验，且必须相对简单波动/成交强度基准证明增益。
- [行业信息扩散](https://academic.oup.com/rfs/article-abstract/20/4/1113/1615954) 与后续 [重新检验](https://www.sciencedirect.com/science/article/pii/S0927539815001012) 的结论并不一致。因此“涨停龙头 → 同板块滞后股”只可作为点时同源映射下的候选挖掘，必须在本地滚动样本外检验；不可按中文板块名连边或固定写进提醒规则。

概率的统计契约也随之收紧：小样本先用 [Beta calibration](https://proceedings.mlr.press/v54/kull17a) 或保守 sigmoid，校准器只能在按完整交易日切分的 OOF 预测上拟合；同时记录 Brier、log loss、可靠性曲线、独立交易日和事件数。多窗口/多阈值试验须登记并在 P3 采用 [stepwise data-snooping 校正](https://onlinelibrary.wiley.com/doi/10.1111/j.1468-0262.2005.00615.x)。在这些条件未满足前，飞书中的数值只能叫“历史条件基准率”，不能称为事件条件概率。

### 5.2 采用、降级和禁止的研究结论

- [波动率管理组合](https://www.nber.org/papers/w22208) 只支持把高波动作为**风险预算收缩候选**；后续样本外反证表明它不能被当成稳定 Alpha。因此现有 `live_policy` / 纸面组合只可据此阻断或缩小建议仓位，不能因为波动率指标而生成 entry。
- 日内动量、短时反转、板块广度和流动性应按连续竞价时段分别评估：开盘、午前、午后及尾盘不共用阈值；午休和隔夜也不得作为 5/15/30 分钟标签的一部分。这个约束已由同段结算时钟实现，未来 replay 必须复用同一个时钟。
- 五档快照只能提供队列不平衡、深度与侵蚀的**观测代理**。只有同时具备逐事件委托、成交方向和撤单时，才允许研究真 OFI；届时还必须证明其相对成交量/波动基线存在本地、滚动样本外增益。没有增益就回退到 evidence-only。
- VPIN、Kyle lambda、CORD 仅能进入 P2 的流动性压力或背离实验池。VPIN 同时存在“对未来波动预测弱、与成交强度机械相关”的公开反证，因此不得成为方向、胜率或入场加分因子。
- 板块龙头到滞后股的扩散只在精确 `(taxonomy_key, sector_key)`、点时成员和同一行情时间轴下做候选挖掘；目标股必须从龙头/广度计算中排除。它需要本地滚动样本外检验，不能固定成长期规则，更不能按中文名称连边。
- 任何概率都必须写清目标，例如“未来 15 个交易分钟先达到目标而非先触发失效”。入场和减仓/离场分别建标签；共享一个泛化“上涨概率”或把分数线性映射成概率，均禁止。

研究记录还必须保留所有尝试过的窗口、阈值和失败实验，并按完整交易日聚类做多重检验校正。这样才不会因同一日多只股票或多种参数重复试验而把偶然结果误作独立样本。

## 6. 晋级与暂停条件

```text
研究证据 → 历史/本地事件回放 → forward shadow → 观察池提醒 → 纸面组合
```

- 每个动作/期限至少 60 个独立交易日、200 个独立 episode，且每 cohort 至少 30 条；当前均未达到。
- 需要样本外净收益、MAE/MFE、成本后期望值、概率可靠性和触发频率漂移共同通过，才可人工批准为 challenger。
- 历史日线/分钟回填、walk-forward 大规模验证、策略自动调参、自动降级和 RL/contextual bandit 仍**暂停**，等待用户明确授权历史数据范围；绝不因为当前前向小样本而提前启动。
- 所有路径保持 `research_only` / `manual_review`；系统没有、也不会自行添加自动下单。

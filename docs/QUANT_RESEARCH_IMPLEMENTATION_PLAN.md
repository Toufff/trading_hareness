# 分析师频道驱动的量化研究平台实施计划

状态：本地基础链路与盘中/分析师联合研究骨架已实施并验收；真实行情历史回填、分钟回放、provider 新授权和 Qlib 实验按用户边界暂缓，未将研究门禁伪装为已完成。
范围：A 股日频研究、盘后生成次日候选池；不接券商下单，不承诺收益  
参考快照：`/Users/papa/codebase/trading/README.md`

## 1. 目标与产品边界

当前系统已经能把飞书、手动网页以及不同分析师频道的文字、图片、音频和视频可靠归档，并在 `quant-research` 中形成基础分析师信号。下一阶段不是再造一个通用行情站，而是建立独有的“观点资产化”链路：

```text
远端 47 服务已解析的分析师报告
  -> 增量同步、版本和哈希校验
  -> 可回溯观点、主题、标的、方向、周期和条件
  -> 与当日市场、板块、基本面和风险事件做时间点一致的验证
  -> 生成次日研究候选、观察池与禁入池
  -> 次日及后续多个周期自动归因
  -> 更新分析师在不同主题、市场状态和预测周期下的可信权重
```

输出是研究候选，不是自动交易指令。每个候选必须给出证据、数据时间、来源质量、风险条件和失效条件；数据不完整时降低置信度或停止生成，不能用估算值掩盖缺失。

## 2. 参考项目取舍

| 项目 | 借鉴 | 明确不照搬 |
| --- | --- | --- |
| `daily_stock_analysis` | provider 优先级、请求级 fallback、连续失败熔断、last-good cache、`source/fallback/stale/fetch_failed` 状态、数据源诊断页 | 大而全的多市场功能、与本项目重复的采集和通知系统 |
| `TradingAgents-CN` | 市场/基本面/新闻/情绪分工、看多/看空证据对照、风险裁决、结果复盘 | `app/` 和 `frontend/` 专有代码；无界 LLM 辩论；当前始终选择主源的 no-op 一致性检查 |
| `investment_data` | 每个来源单独保存、canonical/final 层、复权口径对齐、交易日新鲜度门禁、不可变输入和可复现产物 | Dolt/GitHub Release 全套发布体系；本地 PostgreSQL 足以支撑第一阶段 |
| `adata` | 概念、热点、资金流、龙虎榜和风险事件的公开源能力映射；轻量请求重试 | 将网页接口当稳定 API、代理池规避限制、把未经许可的抓取作为关键主链路 |
| `qlib` | `DatasetH`/Alpha158 风格特征、实验记录、滚动训练、`SignalRecord`、`PortAnaRecord`、交易约束回测 | 直接采用其公开 A 股数据；一开始就引入高频/RL；用回测最好结果挑模型 |

## 3. 初始基线与当前剩余缺口

现有 `quant-service` 是可用骨架，但还不是可靠的量化研究平台：

- 初始快照曾只有 Tushare 单源、逐标的串行同步；现已建立 provider registry、批量事务、有限重试、共享限频、熔断和健康状态。历史回填仍按用户授权暂停。
- `quant.market_bars_daily` 以 `(symbol, trading_date)` 为主键，第二来源会覆盖第一来源，无法保留多源证据和差异。
- `mean_excess_return` 的基准收益口径已修正并纳入研究测试；仍需在历史横截面补齐后重新评估统计稳定性。
- 分析师消息/报告已进入带 `received_at`、版本和证据链的研究层；公司简称、条件句、目标价、风险线和观点变化仍需要更完整的主数据映射与人工复核。
- 当前生产评分已包含量价、板块、资金与数据质量门禁；分析师权重仍锁为研究上下文，行业拥挤度与长期样本校准仍待历史回放。
- 初始 16:45 单体调度已收敛为交易日历门禁、18:50 日线流水线和 18:55–19:10 盘后重跑窗口，并保留 latest-attempt/latest-completed 语义。
- point-in-time 观察日成员、A 股 T+1/涨跌停/停牌/费用/滑点的纸面执行契约已落地；但严格的历史 point-in-time 数据集、事件时钟回放、walk-forward、DSR 和模型版本比较仍待历史数据授权后执行。
- 前端现已展示数据源/实时健康、分析师研究、候选证据、策略漏斗和纸面组合板块暴露；历史回放与分析师晋级面板仍受数据门禁约束。

## 4. 目标架构

```text
Feishu / Relay / future source adapters
                 |
                 v
        ingestion ledger + media assets
                 |
       remote report sync and normalization
                 |
                 v
     analyst evidence and normalized claims
                 |
        +--------+---------+
        |                  |
        v                  v
 market-data providers   official events
 Tushare/AK/Bao         CNInfo/exchanges
        |                  |
        +--------+---------+
                 v
 raw observations -> validation -> canonical PIT data
                 |
                 v
 deterministic features + analyst features
                 |
        +--------+---------+
        |                  |
        v                  v
 rules baseline       Qlib experiments
        |                  |
        +--------+---------+
                 v
 candidate/risk/watch lists + evidence envelope
                 |
                 v
 Vue research console / n8n notifications / attribution
```

职责边界：

- n8n 只做时间编排、阶段触发、通知和失败路由，不承载指标计算或大批量行情数据。
- adapter 只负责内容接入、流式媒体和本地幂等，不承担投资判断。
- `quant-research` 负责远端报告同步、provider、数据质量、信号、特征、评分、回测和查询 API。
- PostgreSQL 是运营真源；Qlib 使用按版本导出的不可变数据快照，不直接修改线上表。
- LLM 负责从非结构化内容抽取候选声明和生成解释；价格、收益、风险过滤与最终分数由确定性代码计算。

## 5. 数据模型

### 5.1 市场原始层

新增表：

- `quant.providers`：provider、能力、优先级、授权类型、速率限制和启停状态。
- `quant.provider_health`：能力维度的最后成功/失败、连续失败、熔断截止、延迟和行数。
- `quant.fetch_runs`：一次拉取的请求参数、交易日、状态、重试、错误分类、开始/结束时间。
- `quant.raw_market_observations`：`provider + capability + symbol + observed_at + payload_sha256` 唯一，保存标准字段和原始 JSON。
- `quant.raw_events`：公告/新闻/题材事件的来源、发布时间、系统可用时间、URL 和内容摘要。

原始层 append-only；修正只能增加新版本，不能覆盖历史证据。

### 5.2 标准层

- `quant.canonical_bars_daily`：校验后的 OHLCV、成交额、复权因子和选用来源。
- `quant.canonical_daily_basic`：PE/PB/市值/换手率等盘后快照。
- `quant.canonical_limits`：涨跌停、停牌、ST 和上市状态。
- `quant.canonical_memberships`：指数、申万行业、概念主题及生效区间。
- `quant.data_quality_issues`：缺行、重复、字段冲突、越界、复权断点和 stale 记录。
- `quant.data_snapshots`：研究/回测使用的数据截止时间、源版本和内容哈希。

标准表必须同时保存 `available_at` 和 `effective_at`，避免回测看到当时尚未公布的数据。

### 5.3 分析师观点层

把现有 `analyst_signals` 扩展为“证据”和“声明”分离：

- `quant.analyst_evidence`：远端 analyst/report ID、报告版本和内容哈希、文本片段、section/material 引用、远端 URL 和同步时间。
- `quant.analyst_claims`：标的、行业或市场级对象，方向、强度、预测周期、条件、目标区间、止损/失效条件、抽取置信度和模型版本。
- `quant.claim_revisions`：同一分析师对同一对象的新增、确认、弱化、反转和撤回。
- `quant.analyst_profiles`：频道、身份、擅长主题、默认周期和状态；来源仍由 `source-registry.json` 配置驱动。
- `quant.analyst_scorecards`：按分析师 × 主题 × 市场状态 × 周期统计样本数、命中率、超额收益、IC、校准误差和最大不利变动。

一条观点可以只指向行业或主题，不强迫映射到股票。股票候选由“观点主题 -> 当日主题成分 -> 量化过滤”产生，并保留这条传播路径。

### 5.4 特征、实验和候选层

- `quant.feature_sets` / `quant.feature_values`：特征版本、可用时间、窗口和数值。
- `quant.experiments`：训练区间、验证区间、测试区间、数据快照、参数、代码版本和结果。
- `quant.model_registry`：仅登记通过门禁的模型；保存 champion/challenger 状态。
- `quant.recommendation_runs`：数据快照、规则/模型版本、市场状态、完整性和降级状态。
- `quant.recommendations`：排名、研究决策、分数分解、证据、风险、有效期和失效条件。
- `quant.outcomes`：1/5/20/60 日收益、基准收益、行业收益、最大有利/不利变动和实际可交易性。

## 6. Provider 与数据质量设计

统一 contract 不返回裸 DataFrame，而返回数据和质量信封：

```python
ProviderResult(
    provider="tushare",
    capability="daily_bar",
    as_of="2026-08-08T18:30:00+08:00",
    rows=[...],
    completeness=0.998,
    freshness="fresh",
    fallback_from=[],
    warnings=[],
    request_id="...",
)
```

第一批 provider：

| 能力 | 主源 | 校验/降级源 | 规则 |
| --- | --- | --- | --- |
| 交易日历、证券列表 | Tushare/交易所 | BaoStock | 当日必须与 SSE 日历一致 |
| 日线、复权因子 | Tushare | BaoStock、AKShare | OHLC、成交量、复权断点逐项校验 |
| 日度估值/换手 | Tushare | AKShare | 缺失时标记 partial，不伪造 |
| 涨跌停/停牌/ST | Tushare + 交易规则 | BaoStock/AKShare | 作为候选生成硬门禁 |
| 行业/概念 | Tushare 申万 | AData/AKShare 公共题材 | 官方行业与网站概念分开建模 |
| 资金流/热度/龙虎榜 | AData/AKShare | 无强保证 | 只作软特征；失败不阻断基础候选 |
| 公告 | CNInfo/交易所 | Tushare 事件接口 | 巨潮清单已作为免费事件源接入；按发布时间做 PIT |

运行策略：

- provider 优先级按 `capability + market` 配置，不设置一个全局顺序。
- 失败采用有上限的指数退避；连续三次失败后短期熔断，半开探测恢复。
- 只有字段满足 schema、交易日正确、行数通过阈值才算成功；HTTP 200 不等于成功。
- 免费网页源限并发并使用本地缓存；不使用代理池绕过访问限制。巨潮公告、东方财富、腾讯、新浪和 BaoStock 都保留来源标签，不能单独解锁推荐。
- canonical 合并不做简单“主源覆盖”：记录逐字段选择理由、差异和置信度。
- 当交易日、基准、涨跌停或候选股票日线不完整时，整次 recommendation run 标为 blocked。

## 7. 分析师频道的独特能力

### 7.1 远端报告同步与声明规范化

远端 47 的“市场复盘档案 API”已负责媒体上传、解析和报告生成。本地不部署 OCR、ASR、视频帧抽取或媒体模型，也不读取媒体临时文件。本地按 Bearer 认证调用：

1. `GET /api/v1/analysts` 同步远端分析师目录。
2. `GET /api/v1/analysts/{analyst_id}/reports?offset=` 增量读取报告清单。
3. 仅当 `report_id + version + content_hash` 发生变化时，调用 `GET /api/v1/analysts/{analyst_id}/reports/{date}` 获取已解析详情。
4. 将 `raw_markdown`、`sections`、`mentioned_stocks`、`mentioned_sectors`、`predictions`、`materials` 和远端版本作为可追溯证据保存。
5. 再用规则/证券主数据规范化标的、行业和主题；低置信度或互相矛盾的映射进入人工复核队列。

抽取对象至少包括：

```text
scope: market | sector | theme | stock
direction: bullish | bearish | neutral | avoid
horizon: next_day | 5d | 20d | 60d
thesis: 核心逻辑
catalysts: 催化剂
conditions: 生效条件
invalidation: 失效条件
targets: 明确提及的标的/板块/价位
confidence: 抽取置信度，不是投资胜率
```

### 7.2 分析师权重

不使用一个永久总胜率。每位分析师按以下切片动态估计：

- 预测周期：次日、5 日、20 日、60 日；
- 对象：市场、行业、主题、个股；
- 市场状态：趋势、震荡、高波动、风险收缩；
- 观点方向：看多、看空、回避；
- 样本可信度：明确标的和条件高于泛泛评论；
- 时效：新表现权重大，但使用衰减而非清空历史。

样本不足时向总体均值收缩，避免某位分析师两次命中就获得极高权重。评分只使用当时已经完成的历史结果，防止未来泄漏。

### 7.3 证据对照而非无界辩论

对每个候选生成固定的四块证据：

- 支持：哪些分析师、市场/行业/价格特征支持；
- 反对：反向观点、过热、基本面或公告风险；
- 可交易性：停牌、涨停、流动性、T+1 和次日开盘跳空风险；
- 失效条件：价格、成交量、事件或时间条件。

LLM 只能总结这四块结构化输入，不能自行修改确定性分数或隐藏反对证据。

## 8. 盘后日常流程

调度以 `Asia/Shanghai` 交易日历为准：

| 时间 | 阶段 | 结果 |
| --- | --- | --- |
| 持续 | 远端分析师报告增量同步 | evidence/claims 可增量更新 |
| 15:10 | 收盘状态初检 | 记录交易日和待处理量，不生成候选 |
| 17:30 | 主源行情、估值、涨跌停同步 | raw 数据和 fetch run |
| 18:10 | 备源交叉校验与公告同步 | canonical 数据、质量差异 |
| 18:30 | 分析师声明封账 | 固定本次 run 的 `knowledge_cutoff` |
| 18:40 | 特征计算和市场状态识别 | 版本化 feature snapshot |
| 18:50 | 规则基线 + champion 模型评分 | candidate/watch/no-trade |
| 19:00 | 风险门禁与解释生成 | 带完整 evidence envelope 的报告 |
| 19:10 | n8n 推送摘要 | 失败则进统一错误和重试流程 |
| 次日收盘后 | 1 日归因，之后滚动补 5/20/60 日 | outcomes 和 scorecards |

每个阶段都有独立 job ID、幂等键和可重跑 API。后阶段只消费已完成的前阶段快照，不把所有工作塞进一个 120 秒 HTTP 请求。

## 9. 评分与风险门禁

第一版先建立可解释规则基线：

```text
总分 = 分析师共识 25%
     + 分析师条件化能力 15%
     + 行业/主题相对强度 15%
     + 个股趋势与量价 15%
     + 基本面/估值质量 10%
     + 市场状态适配 10%
     + 催化/公告 10%
     - 风险和数据质量惩罚
```

硬过滤：ST/*ST、停牌、退市整理、上市样本不足、关键数据 stale、次日不可合理成交。涨停不是简单永久排除，但必须降级为 watch 并标注无法按收盘价成交。

软风险：近端大幅跳涨、波动率、流动性、热点拥挤、解禁、减持、业绩预告、监管事件、分析师观点过度集中和跨源数据冲突。

市场状态至少用沪深 300/中证 1000 趋势、全市场涨跌宽度、成交额和波动率，不再只比较沪深 300 的两个收盘价。

## 10. Qlib 研究与回测

Qlib 作为离线实验容器，不进入日常采集主链：

1. 从 canonical PIT 表导出带 snapshot ID 的 Qlib 数据集。
2. 先实现规则基线和分析师特征，再加入 Alpha158 子集；不一开始使用全部特征。
3. 标签使用未来 1/5/20 日相对基准收益，并把涨跌停、停牌和退市样本纳入。
4. 使用 expanding/rolling walk-forward；训练、验证、测试按时间严格隔离。
5. 回测计入佣金、印花税、滑点、100 股整手、T+1 和无法成交。
6. 记录 IC/RankIC、命中率、超额收益、最大回撤、换手率、容量和分市场状态表现。
7. champion/challenger 同时跑；新模型连续多个窗口优于基线且风险不恶化才晋级。

严禁用全部历史反复调参后只汇报最佳结果。所有实验必须绑定数据快照、代码版本、参数和随机种子。

## 11. API 与服务拆分

计划将当前单文件 `main.py` 拆为：

```text
quant-service/app/
  api/                 health, providers, data, claims, runs, recommendations
  domain/              schemas and scoring rules
  providers/           tushare, akshare, baostock, cninfo, adata optional
  repositories/        PostgreSQL access
  services/
    ingestion/         raw write and normalization
    quality/           validation and canonicalization
    extraction/        remote report normalization and claim revisions
    features/          PIT feature computation
    scoring/           rules and model inference
    attribution/       outcomes and analyst scorecards
    experiments/       Qlib snapshot/export/import
  workers/             bounded background jobs
```

关键 API：

- `POST /api/v1/fetch-runs`：创建能力/交易日同步任务；返回 run ID。
- `GET /api/v1/fetch-runs/{id}`：阶段、provider、重试和质量结果。
- `POST /api/v1/canonical-runs`：对指定 raw snapshot 校验并封账。
- `POST /api/v1/extraction/jobs/{analysis_id}`：幂等抽取证据与声明。
- `POST /api/v1/research-runs`：从已封账的数据和知识截止时间创建研究任务。
- `GET /api/v1/research-runs/{id}`：完整状态和阻断原因。
- `POST /api/v1/events/cninfo/sync`：同步指定股票或核心股票池的巨潮公告清单，写入事件证据。
- `POST /api/v1/market/sectors/concepts/research/run`：执行“高流入概念 -> 涨停候选 -> 个股研究 -> 公告补充”的研究扫描。
- `GET /api/v1/recommendations/latest?as_of_date=`：候选、证据和质量信封。
- `GET /api/v1/analysts/{id}/scorecard`：条件化表现和样本量。
- `GET /api/v1/providers/health`：能力维度健康和熔断状态。

旧 `/pipeline/daily` 保留为兼容编排入口，但内部只创建阶段任务并返回 run ID，不同步等待全流程结束。

## 12. n8n 工作流设计

拆成可独立重跑的工作流：

1. `研究入口：分析师内容抽取`：消费完成的 ingestion analysis job。
2. `盘后研究：交易日门禁`：判断是否为交易日并创建 daily research run。
3. `盘后研究：主源同步`：触发 fetch run 并轮询完成。
4. `盘后研究：交叉校验与封账`：生成 canonical snapshot。
5. `盘后研究：候选生成`：触发 feature/scoring。
6. `盘后研究：报告与推送`：只消费成功结果。
7. `盘后研究：结果归因`：每日回填 1/5/20/60 日表现。
8. `盘后研究：数据源巡检`：监控 stale、熔断、缺行和积压。

47 远端报告的 Bearer token 保留在 n8n 已有凭据中，只由同步工作流的 HTTP 节点使用，不能进入 workflow JSON、`quant-research` 环境变量、前端或日志。n8n 的执行数据不保存大批量行情响应，只保存 run ID 和摘要。

## 13. Vue 研究控制台

保留现有 Vue 3 + Vite 工程，新增 `/research` 路由和以下页面：

- 今日总览：市场状态、候选/观察/禁入、数据截止时间和 run 状态。
- 候选详情：分数瀑布、支持/反对证据、行情图、风险和失效条件。
- 分析师画像：按周期/主题/市场状态的表现、样本量、近期观点和修订历史。
- 频道时间线：远端报告、结构化声明和后续结果可互相跳转。
- 数据源 Doctor：provider 健康、熔断、最近成功、差异、stale 和手动重跑。
- 研究实验：规则基线与 Qlib challenger 的离线指标；不提供实盘下单按钮。

前端只展示后端结构化结果，不在浏览器计算最终评分。表格支持筛选和分页，图表按需加载，SSE 只推 run 状态变化。

## 14. 可观测性、安全和资源控制

- Prometheus 指标：provider 成功率/延迟/熔断、raw/canonical 行数、质量冲突、抽取积压、run 耗时、候选数量和归因完成率。
- 每个结果可沿 `recommendation -> snapshot -> canonical -> raw -> provider request` 回溯。
- 原始媒体继续由远端档案与现有 adapter 管理；本地只保存远端已解析报告的必要证据、哈希和版本，不保存媒体副本。
- 行情批处理采用有界并发与流式数据库写入；不把全市场 DataFrame 常驻进程内存。
- provider token 不记录到日志、数据库 payload、前端或 n8n execution。
- 服务器公开时 `/research` 需要统一认证；`quant-research` 端口继续只走 Docker 内网。

## 15. 实施阶段与验收

### A. 基线冻结与测试护栏

工作：备份 PostgreSQL 和 workflow；为当前抽取、评分、API 增加回归测试；修正 `mean_excess_return` 命名/算法；把 16:45 单体流程改为兼容的异步 run 骨架。  
验收：旧采集链路不受影响；同一输入幂等；现有 API 有兼容测试；可回滚到当前镜像和数据库备份。

### B. Provider registry 与 raw 层

工作：实现 contract、Tushare 批量/历史同步、AKShare 和 BaoStock 适配器、fetch run、重试/熔断、原始层。  
验收：任一备源失败不拖垮批次；HTTP 成功但空数据会失败；每条原始数据能找到 provider 和请求。

### C. Canonical 与质量门禁

工作：逐字段校验、复权因子对齐、交易日/行数/价格边界检查、差异表、snapshot。  
验收：双源人工构造冲突能被检测；不会覆盖原始值；关键数据缺失时研究 run 被明确阻断。

### D. 远端观点资产化

工作：远端分析师目录/报告增量同步、evidence/claims/revisions、公司别名与主题映射、远端已解析字段规范化和复核队列。  
验收：远端新报告、同版本重复同步、版本更新和分页各有端到端样本；每条声明可跳回远端报告证据；重跑不重复创建。

### E. 规则基线候选池

工作：PIT 特征、条件化分析师权重、市场状态、硬/软风险、证据对照、分阶段盘后 workflow。  
验收：结果可重复；无未来数据；ST/停牌/缺数据过滤正确；每个分数可解释。

### F. 结果归因与分析师画像

工作：1/5/20/60 日 outcome、相对基准/行业收益、MAE/MFE、贝叶斯收缩/置信区间、观点修订效果。  
验收：样本不足不会得到极端权重；历史重算有方法版本；分析师页面与明细一致。

### G. Qlib 回测与模型注册

工作：不可变数据导出、Alpha158 子集、walk-forward、交易约束、实验记录和 champion/challenger。  
验收：同 snapshot/seed 可复现；报告含成本和不可成交；模型不能绕过规则风险门禁。

### H. 研究控制台

工作：Vue 路由、总览、候选、分析师、频道时间线、Doctor 和实验页；API typed client 与组件测试。  
验收：移动端基本可用；断线有提示；大列表分页；任何候选能在三次点击内追溯到原内容。

### I. 生产化与服务器一键部署

工作：worker 资源限制、Prometheus、备份/恢复、数据保留、server compose/Caddy 路由、部署 smoke 和故障演练。  
验收：单 provider 故障、数据库重启、worker 重启和重复调度均可恢复；公网只暴露受保护的 UI/API；文档可在新服务器一键部署。

## 16. 测试矩阵

- Provider contract：正常、空响应、字段漂移、限流、超时、部分数据和熔断恢复。
- 数据质量：重复、缺日、OHLC 非法、成交量单位、复权断点、跨源容差和未来发布时间。
- 声明规范化：远端标的/行业/主题、否定、条件、观点反转、多标的、版本更新和分页增量。
- 评分：固定 fixture 的分数快照、硬过滤、数据质量惩罚和市场状态切换。
- 回测：T+1、涨跌停无法成交、停牌、退市、费用、滑点、整手和无未来泄漏。
- 工作流：每阶段幂等、部分失败重跑、Error Trigger、超时和状态对账。
- 前端：API schema、加载/空/失败/stale、SSE 重连、筛选分页和证据跳转。
- 部署：全新服务器启动、备份恢复、升级回滚、volume 配额和认证。

## 17. 首轮实现顺序

实际编码按 `A -> B -> C -> D -> E -> F -> H -> G -> I` 推进。先让数据、证据和规则基线可靠，再做机器学习；研究控制台在规则基线后即可投入日常使用，不必等待 Qlib 模型。

第一轮修改预计集中在：

```text
quant-service/app/main.py             -> 薄 API 入口
quant-service/app/database.py         -> 迁移拆分和新表
quant-service/app/providers/*         -> provider contract/adapters
quant-service/app/services/*          -> quality/extraction/features/scoring
quant-service/tests/*                 -> contract/PIT/scoring tests
scripts/build-quant-*-workflow.mjs     -> 分阶段 n8n workflow
frontend/src/*                        -> /research 控制台
compose.yaml / deploy/*               -> worker、指标和部署配置
DEPLOYMENT.md / OPERATIONS.md          -> 运维、恢复和服务器部署
```

任何阶段都不能改变现有飞书/relay 媒体上传的远端接口契约。量化研究失败只影响新候选生成，不影响原始内容归档。

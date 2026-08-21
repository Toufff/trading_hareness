# AKShare 能力缺口与互补接入分析

日期：2026-08-20。结论基于当前容器内 `akshare==1.18.93` 的实际函数清单和 AKShare 官方文档。AKShare 是公开源聚合库，官方文档覆盖股票、期货、债券、期权、基金、指数、宏观、外汇、能源、另类等大类，但其特别声明把数据定位为研究参考，并提示部分接口会因上游变化而失效；因此本平台只把它作为 `provider_key=akshare` 的补充/交叉验证层，不替代主源、super 或持牌数据。

参考：

- AKShare 官方文档：<https://akshare.akfamily.xyz/>
- AKShare 数据目录：<https://akshare.akfamily.xyz/data/index.html>
- AKShare 特别说明：<https://akshare.akfamily.xyz/special.html>

## 当前覆盖概况

容器反射结果：

| 类别 | 当前安装包可调用函数数 |
|---|---:|
| 全部 callable | 1,102 |
| `stock_*` 股票相关 | 407 |
| `fund_*` 基金/ETF | 74 |
| `index_*` 指数 | 79 |
| `bond_*` 债券 | 44 |
| `futures_*` 期货 | 66 |
| `option_*` 期权 | 46 |
| `macro_*` 宏观 | 226 |

## 2026-08-20 升级后实测

测试范围是本服务实际接入的 70 个 AKShare 底层函数，而非把安装包中全部 1,102 个可调用对象无参数并发执行。后者包含不同市场、参数合同、付费资格和历史下载接口，既不能代表生产覆盖，也会不必要地触发公开上游限流。测试以 `000636.SZ` 和最近完整交易日 `2026-08-20` 为样本，单请求顺序执行并保留来源差异。

| 结果 | 数量 | 说明 |
|---|---:|---|
| 返回有效表格 | 60 | 日线、腾讯全 A、同花顺目录、行业/概念资金流、涨跌停池、龙虎榜、大宗交易、公司事件、指数基金、宏观/利率/商品/期权均通过。 |
| 上游暂不可用 | 10 | 均已明确记录，不会被标记为“完整覆盖”。 |
| 本地签名兼容问题 | 3，已修复 | `stock_dzjy_sctj`、`stock_dzjy_yybph`、`stock_dzjy_hygtj` 在 1.18.93 不再接受日期范围；适配器已改为新版无日期/近一月参数。 |

当前不可用函数及现象：

- 东方财富板块目录和精确成员：`stock_board_concept_name_em`、`stock_board_industry_name_em`、`stock_board_concept_cons_em`、`stock_board_industry_cons_em` 返回上游断连。THS 目录与东财板块涨跌幅仍可用；缺精确成员时继续按覆盖门禁失败关闭，不能生成“全板块 Top10”。
- 东方财富个股资金流：`stock_individual_fund_flow`、`stock_individual_fund_flow_rank`、`stock_main_fund_flow` 返回上游断连；全市场即时资金流、行业和概念资金流可用。
- 分析师/热度补充：`stock_profit_forecast_em` 返回空结构，`stock_rank_forecast_cninfo` 返回缺失 `records`，`stock_hot_rank_em` 返回上游断连；同花顺盈利预测、最新热度、新闻和评论可用。

这批失败应当以“上游暂不可用”处理：保留已成功的独立补充项、按照 provider 熔断与限频降级，不把失败接口的零行误写成市场零值，也不以中文名称猜测板块成员。

已落地的 AkShare 能力：

| 能力 | 已接入函数 | 入库位置 | 决策边界 |
|---|---|---|---|
| 公开日线 | `stock_zh_a_hist`，fallback `stock_zh_a_hist_tx` | canonical 日线交叉验证 | 不覆盖主源/super |
| 市场总貌 | `stock_sse_summary` | `raw_market_observations` | 市场状态参考 |
| 龙虎榜事件 | `stock_lhb_detail_em` | `market_events` | 事件证据，盘后才可用 |
| 强势股池 | `stock_zt_pool_strong_em` | `market_events` | 情绪候选，不单独推荐 |

本轮新增互补能力包：

| 能力包 | 主要函数 | 补齐的缺口 |
|---|---|---|
| `market_breadth` | `stock_sse_summary`、`stock_szse_summary`、`stock_a_high_low_statistics`、`stock_a_below_net_asset_statistics`、`stock_account_statistics_em` | 交易所级市场宽度、破净/新高新低、账户统计 |
| `board_taxonomy` | `stock_board_concept_name_em`、`stock_board_concept_cons_em`、`stock_board_industry_name_em`、`stock_board_industry_cons_em`、THS 目录、板块异动 | 东财/同花顺概念行业目录和成分，补 `ths_member` 批次不足 |
| `moneyflow_supplement` | `stock_fund_flow_individual`、`stock_individual_fund_flow`、`stock_main_fund_flow`、`stock_fund_flow_industry`、`stock_fund_flow_concept`、`stock_hsgt_fund_flow_summary_em` | 个股、行业、概念、北向和主力资金公开源交叉验证 |
| `limit_pool` | `stock_zt_pool_em`、`stock_zt_pool_previous_em`、`stock_zt_pool_zbgc_em`、`stock_zt_pool_dtgc_em`、`stock_zt_pool_sub_new_em` | 涨停、昨日涨停、炸板、跌停、次新情绪池 |
| `lhb_supplement` | `stock_lhb_stock_statistic_em`、`stock_lhb_jgmmtj_em`、`stock_lhb_jgstatistic_em`、`stock_lhb_yybph_em`、`stock_lhb_traderstatistic_em` | 龙虎榜机构、席位和游资统计 |
| `block_trade_supplement` | `stock_dzjy_mrmx`、`stock_dzjy_mrtj`、`stock_dzjy_sctj`、`stock_dzjy_yybph`、`stock_dzjy_hygtj` | 大宗交易明细、市场统计、营业部和行业统计 |
| `corporate_risk_supplement` | `stock_cg_lawsuit_cninfo`、`stock_cg_guarantee_cninfo`、`stock_cg_equity_mortgage_cninfo`、`stock_repurchase_em`、`stock_dividend_cninfo`、`stock_allotment_cninfo`、`stock_gddh_em` | 诉讼、担保、股权质押、回购、分红配股、股东会 |
| `analyst_heat_supplement` | `stock_analyst_rank_em`、`stock_profit_forecast_em`、`stock_profit_forecast_ths`、`stock_hot_rank_em`、`stock_news_em`、`stock_comment_em` | 公开分析师排名、盈利预测、热度、新闻和评论 |
| `index_fund_supplement` | `index_csindex_all`、`index_stock_cons`、`index_stock_cons_csindex`、`index_stock_cons_weight_csindex`、`index_component_sw`、`fund_portfolio_hold_em`、`fund_report_stock_cninfo` | 指数成分/权重、申万成分、基金持仓 |
| `macro_cross_asset_supplement` | `macro_china_cpi`、`macro_china_gdp`、`macro_china_money_supply`、`bond_zh_us_rate`、`rate_interbank`、`futures_zh_spot`、`option_risk_indicator_sse`、`energy_oil_hist` | 宏观、利率、商品、期权风险指标；默认不随单票按钮运行 |

## 还缺的能力与处理策略

| 缺口 | AKShare 是否有补充 | 处理策略 |
|---|---|---|
| 实时分钟/盘口 | 有 `stock_zh_a_spot_em`、`stock_bid_ask_em`、`stock_zh_a_tick_tx_js` 等 | 当前主源/super 实时仍优先；AkShare 只做盘中观察，不进入推荐硬门槛 |
| 全量概念成分 | 有东财/THS 概念、行业目录和成分 | 交互 probe 只取少量板块；全量用后台批次，保留 `partial` |
| 主力/散户资金 | 有东财个股、主力、行业、概念资金流 | 用来交叉验证 Tushare `moneyflow_dc`，字段不混写 |
| 筹码分布 | 有 `stock_cyq_em` | super 的 `cyq_perf/cyq_chips` 优先；AkShare 后续可做补充展示 |
| 量化因子 | 有估值、市场 PE/PB、评论热度，另有大量技术榜单 | 先只落原始证据；进入因子实验前必须定义字段合同和 PIT 可得时点 |
| 盈利预测/分析师 | 有东财/同花顺盈利预测、分析师排名 | 只作为公开补充，不替代远端报告档案里的分析师证据 |
| 机构调研 | AKShare 有部分公告/新闻/研报包装，但稳定性不如主源 | 仍以主源/super 与公告归档为主，AkShare 做补充 |
| 游资数据 | 龙虎榜席位、营业部排名可补 | 已纳入 `lhb_supplement` |
| 涨停榜单/热榜 | 涨停池、强势股池、热股排行可补 | 已纳入 `limit_pool` 与 `analyst_heat_supplement` |
| 指数成分权重 | 中证、申万、Sina 等路径可补 | 已纳入 `index_fund_supplement`，但训练使用前需要公告日/生效日校验 |
| 基金持仓 | 有基金持仓、行业配置、ETF/REITs | 当前只小样本探测；后续按季度批次补，不做历史全量默认下载 |
| 宏观商品衍生品 | 宏观 226 个函数，另有债券、利率、期货、期权、能源 | 建市场状态层，不跟单票 probe 混跑 |

## 使用原则

1. 主源/super 能稳定提供的数据继续优先，AkShare 不静默覆盖。
2. AkShare 数据必须保留 `upstream_site`、`ak_function`、`available_at` 和原始 payload。
3. 公开源允许失败、空值和字段漂移；失败只降低补充证据，不阻断 P0 行情门禁。
4. 盘后数据不能盘中使用；龙虎榜、完整涨停池、收盘统计、基金持仓和财报类数据必须按可得时点入库。
5. 全量历史仍不默认下载；股票查询、板块查询和事件查询优先按需补齐。

## 2026-08-09 smoke

`000636.SZ`、交易日 `2026-08-07`、`board_limit=3` 的完整股票补充包跑通：

- 日线 25 行。
- 市场总貌 8 行。
- 龙虎榜事件 2,033 行，按上限入库 100 行。
- 强势股池 134 行。
- 市场宽度 363 行。
- 板块/行业/成分 666 行。
- 资金流补充 695 行。
- 涨跌停情绪池 263 行。
- 龙虎榜席位统计 1,500 行，按上限入库 1,000 行。
- 大宗交易 190 行。
- 分析师/热度/新闻 424 行。
- 指数成分/基金持仓 1,875 行，按上限入库 1,000 行。

公司事件风险包最初因 `stock_gpzy_pledge_ratio_detail_em()`、`stock_ggcg_em()` 等全分页扫描超时；指数基金包里的 `fund_etf_spot_em()` 也属于多页现货扫描。上述慢接口已移出默认交互 probe，后续如需要全市场股权质押、高管持股变动或 ETF 全市场快照，应做后台分页任务。

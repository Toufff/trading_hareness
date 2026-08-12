# 超级源 SDK / GET 物理通道能力审计

审计日期：2026-08-11。当前物理源已拆分为 `tushare_super_sdk` 与
`tushare_super_get`；`provider: "super"` 只是向后兼容的能力路由别名，不再
作为健康、限流或能力证据的身份。历史 `tushare_super` 数据库记录仅保留
审计证据且已禁用新路由。

SDK 通道复刻官方 SDK 请求：
`POST {base}/{api_name}`，在 `params` 中补充 `ts_type_name`，并通过私有环境变量
`TUSHARE_SUPER_SDK_*` 使用专用代理。GET 通道使用
`GET {base}/{api_name}` + `X-API-Key` 与 `TUSHARE_SUPER_GET_*`。旧环境变量仍可
作为兼容回退。Token、代理地址和密码不会出现在代码或本文件。

所有平台验收均通过 `POST /api/v1/providers/tushare/fetch`、`provider: "super"`
执行，历史日期为 2022-04-28 至 2022-04-29，单项最多保存 3 行。`partial` 仅表示
响应超过本地 3 行验收上限，不代表供应商错误。

## 已验证非空

| 主题 | 接口 | 平台 API 实测响应 |
| --- | --- | --- |
| A 股日线 | `daily` | 25 行 |
| 开盘 / 收盘集合竞价 | `stk_auction_o`、`stk_auction_c` | 各 1 行 |
| 同花顺板块目录 | `ths_index` | N/I/R/S/ST/BB 六类共 1,481 行 |
| 同花顺板块成分 | `ths_member` | `883300.TI` 返回 319 行 |
| 同花顺行业资金流 | `moneyflow_ind_ths` | 2026-08-07 返回 90 行 |
| 个股资金流 | `moneyflow` | 2 行 |
| 筹码胜率 / 分布 | `cyq_perf`、`cyq_chips` | 各 2 行 |
| 龙虎榜 / 机构 | `top_list`、`top_inst` | 各 75 行 |
| 专业技术因子 | `stk_factor_pro` | 2 行 |
| 卖方盈利预测 | `report_rc` | 1,113 行 |
| 游资名录 | `hm_list` | 110 行 |
| 涨跌停单 | `limit_list_d` | 215 行 |
| 个股热榜 | `kpl_list` | 380 行 |
| 官方扩展行情与分类 | `bak_basic`、`stk_weekly_monthly`、`index_classify`、`index_weekly`、`sw_daily`、`daily_info` | 均返回真实行 |
| 跨资产参考 | `fund_company`、`fut_basic`、`cb_basic` | 均返回真实行 |

`cyq_perf` 的已保存原始证据包含 `600000.SH` 在 2022-04-28、2022-04-29 的真实
`winner_rate` 和 `weight_avg` 数值，不是字段名伪行。

## 按接口择优与完整并集

2026-08-11 对两条物理通道按各自协议、URL 和代理重新实测。`provider: "super"`
会按接口选择更完整或时效更好的物理源；首选调用失败，或下列允许回退的接口返回
合法空集时，才整体调用次源。不同物理源的行和分页永不拼接为一个合成快照，响应会
保留最终 `provider`、`fallback_failures` 和 `fallback_empty_providers` 供审计。

实时路由如下：

| 接口 | 首选 | 兜底 | 实测边界 |
| --- | --- | --- | --- |
| `rt_k`、`rt_min` | City SDK | GET | City 有交易所时间戳；GET 可接续 |
| `rt_min_daily` | GET | 无 | GET 返回开盘至当前完整分钟序列 |
| `rt_etf_k` | City SDK | GET | 两路非空，City 时间戳更明确 |
| `rt_etf_min` | City SDK | 无 | GET 权限拒绝 |
| `rt_idx_k` | City SDK | GET | 两路非空，City 时间戳更明确 |
| `rt_idx_min` | City SDK | 无 | GET 连续返回 relay unavailable |
| `rt_sw_k` | GET | City SDK | City 忽略单代码范围并返回全截面；平台仍会按代码过滤 |
| `rt_fut_min`、`rt_fut_min_daily` | GET | 无 | City 前者合法空集、后者 404 |

`rt_etf_min_daily`、`rt_etf_sz_iopv`、`rt_idx_min_daily` 当前两路均没有已验证可用
候选，路由直接拒绝，不会借“已配置”冒充实时覆盖。主源没有实测可用的实时能力，
不会参与任何实时接口候选。

特色接口以完整度决定顺序：City SDK 优先 `ths_member`（557 对 278）、
`ths_index`（411 对 200）、`top_inst`（62 对 46）以及资金流、筹码、龙虎榜、
涨跌停、板块和事件类接口；GET 优先 `moneyflow_ind_dc`（City 合法空集、GET 1,031）、
`stk_factor_pro`（6 对 5）、`hm_list`（113 对 110）和 `dc_hot`（777 对 774）。
`daily` 固定 GET 首选、主源兜底；`stock_basic` 则保持主源为全 A 名录基准，
GET、City SDK、REST 依次仅作回退/对账。

GET 当前实测白名单还包括 `daily_basic`、`index_daily`、`fut_basic`、`cn_gdp`、
`cyq_perf`、`cyq_chips`、五类个股/行业/市场资金流、`moneyflow_cnt_ths`、
`report_rc`、龙虎榜/游资/涨跌停/热榜/同花顺板块等已逐项得到结构合法响应的接口。
未实测接口不会因为 HTTP 200 或供应商目录声明而自动进入 GET 路由。

## 概念资金流的成分覆盖

`moneyflow_cnt_ths` 与 `ths_member` 共用同花顺概念 `.TI` 代码。平台提供
`POST /api/v1/market/sectors/concepts/members/sync`，以最多 25 个板块一页的方式
同步成分，供后续将概念资金流与个股资金、量比横截面做精确连接。请求被严格分页，
不会在报告读取时临时扫描数百个上游板块。

## 板块映射与涨停关联挖掘的接口分工

当前“涨停关联股”不是把相似中文名称拼接为关联，而是使用同花顺精确成员关系：

| 决策环节 | 接口 / 来源 | 使用边界 |
| --- | --- | --- |
| 概念目录与精确成员 | `ths_index` + `ths_member`（Super City SDK） | SDK 是完整单板块快照路径；GET 大板块会截断，只能作有界回退。|
| 当日概念收盘资金流 | `moneyflow_cnt_ths` | 仅当目标交易日实际返回非空横截面时用于盘后板块强度；盘中合法空集不回退为前日数据。|
| 行业收盘资金流 | `moneyflow_ind_ths` | 同上，仅作收盘/复盘上下文，不能替代盘中实时资金方向。|
| 涨停、梯队、题材归因 | `limit_list_ths`、`limit_list_d`、`limit_step`、`limit_cpt_list` | 当日非空后作为盘后双源核验和连板研究；不把历史实测可用等同于当前盘中已有数据。|
| 盘中涨停事实锚点 | 东财 `stock_zt_pool_em` | 每次五分钟板块快报仅一次有界读取；当前 Tushare 涨停接口为空时采用它，保留来源标签。|
| 同刻个股确认 | 腾讯全 A：主力流、量比、换手、涨跌幅 | 与板块快报共用同一份全 A 快照，不为候选逐股新增扫描。|
| 二次确认 | `rt_k` / `rt_min` / `rt_min_daily`（Super） | 仅对候选做分钟承接、回落和时间戳校验；主源不具备实时能力。|
| 龙虎榜与席位 | `top_list`、`top_inst` | 盘后/次日复核；不能作为当日盘中事后已知信息。|
| 结构性补充 | `daily`、`daily_basic`、`stk_limit`、`suspend_d`、`adj_factor`、`cyq_perf`、`cyq_chips`、`stk_factor_pro` | 日线、交易约束、筹码和技术因子；用于复盘与次日候选，不直接触发盘中买入。|
| 竞价研究 | `stk_auction_o`、`stk_auction_c` | 已验证开放，适合后续 09:20–09:25 的独立竞价模型；通用 `stk_auction` 仍是 declared，未纳入策略。|

关联挖掘当前只保留：非涨停标的、涨幅 `1%–7%`、主力净流入为正、量比至少
1.3、换手至少 1%，且概念本身成员数在 2–200 之间。它是待分钟承接确认的研究候选，
不是追涨指令。这样可避免次新/宽泛概念和已进入加速末段的标的被误列为“可买”。

盘中东财资金流采用另一套板块目录，不能按中文名称与同花顺概念静默混接。对应的
`POST /api/v1/market/sectors/eastmoney/members/sync` 按东财概念/行业代码和同源
成分表分页持久化；只有这一映射完成后，才会生成东财盘中板块的逐板块 Top10。
`POST /api/v1/market/sectors/intraday/report` 使用东财板块流和腾讯全 A 横截面，
按腾讯主力净流字段输出各板块最多 10 个成员，并返回每类板块的成分覆盖率。

GET 网关也会忽略 `stock_basic` 的 `offset`；不传 `limit` 时 2026-08-10
实测返回 5,530 个唯一代码，但主源同口径返回 5,539 个，因此 GET 只作为可访问的
对账/回退来源，不能标成全 A 名录的权威完整源。`all_a` 名录默认优先主源，日线仍由
GET 接管。
同日 `daily(trade_date=20260810)` 一次返回 5,538 个唯一代码。因此这两条
全市场路径不伪造分页，而是校验唯一代码、交易日与最低横截面规模；低于
门槛时整批拒绝。`moneyflow_cnt_ths` 同日实测为 379 个板块，低于接口
单次上限，按日期作为完整横截面。通用 `limit/offset` 仅保留给确认支持的接口；
一旦检测到忽略 `offset`，就失败而不是重复存储首页。

`ths_member` 是例外：[官方文档](https://tushare.pro/document/2?doc_id=261)
的输入只有 `ts_code` 和 `con_code`，并未声明
`limit/offset`。实测 GET 对大板块会截断且忽略 `offset`，因此 GET 只标为
`bounded_only`。完整成员快照走 SDK 单板块调用；SDK 网关当前会拼接两套字段
布局，平台将误放在 `is_new` 的成员名称移回 `con_name`，再按 `con_code`
精确去重。超出 10,000 个唯一成员或调用失败时，该批次不写入，也不关闭历史成员。

运行时预算为 SDK 30 次/分钟；GET 仅在特别关注窗口按全局最小 1 秒间隔启动请求，
其他时段维持低频业务调用。两个 GET key 按主/备故障
转移，不叠加吞吐。这是本地安全配置，不冒充供应商书面硬限额。

## 合法空集

2026-08-11 的最小参数探针曾在 `moneyflow_ind_dc`、`report_rc` 观察到合法空集；
换用当日/区间参数后，两条物理通道又分别返回 1,031 行和 2 行。这说明空集是
**请求级证据**，不能固化为某物理源永久无能力。自动或 `super` 路由只要还有合规
候选就会继续查询；全部候选均空时返回首个合法空响应，并记录所有
`fallback_empty_providers`，不把它伪装成权限或网络错误。

## 盘后一键更新

前端“盘后一键更新”调用 `/api/v1/market/post-close/refresh`，按依赖顺序保存
全 A 基准与日线、腾讯收盘快照、AKShare/东财补充、同花顺资金流、巨潮公告、
板块复盘、分析师结算和盘后策略。每个阶段都有独立状态和时间预算；收盘数据尚未
发布时会返回 `partial` 与 `deferred_stages`，其余已完成证据仍可在前端读取。
同一时间只允许一个批次，重复点击返回 HTTP 409，不会并发混写。

这只说明所测标的/日期没有数据，不能外推为所有日期或所有标的均有数据。

## 未验证或不可用

- `dc_index`、`tdx_index`：当前参数组合分别返回 HTTP 400/参数不能为空，未验收。
- `dc_member`、`tdx_member`、`kpl_concept_cons`：因父级板块代码未取得，尚未验收。
- `stk_factor`：供应商路径返回 HTTP 404。官方 SDK 将 404 静默转换为空 DataFrame，平台不接受这种静默成功，明确标记为失败；应使用已验证的 `stk_factor_pro`。
- 实时扩展边界以 2026-08-11 连续竞价复验为准：ETF、指数、申万和期货中已验证的 7 个 City 实时接口及 8 个 GET 实时接口按上表进入路由；`rt_etf_min_daily`、`rt_etf_sz_iopv`、`rt_idx_min_daily` 仍不可用。
- `stock_company`、`stk_shock`、`dc_concept`、`st`：超级路径当前返回 404 或失败，不能算覆盖。`index_weight` 修正为官方 `index_code` 参数后返回合法零行；主源同一请求返回 300 行。

官方扩展、主源差异和完整实时清单见 [Tushare 官方扩展与双源能力矩阵](TUSHARE_OFFICIAL_CAPABILITY_MATRIX.md)。

## 平台行为

对象行与位置行两种 Tushare 响应都能正确解码；重复“字段名=字段值”行仍会被拒绝，防止失效网关污染原始证据、特征或推荐。

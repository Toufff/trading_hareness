# 外部数据接口验收记录（2026-08-21）

本记录只保存接口能力、时间语义和结果状态，不保存任何 API key、代理凭据或 Bearer token。

## 已实测返回

| 来源 | 接口/能力 | 结果 | 说明 |
|---|---|---|---|
| Tushare Super SDK | `daily`, `daily_basic`, `moneyflow`, `moneyflow_mkt_dc`, `moneyflow_ind_ths`, `limit_step`, `stk_weekly_monthly`, `stk_week_month_adj`, `stk_nineturn`, `index_classify`, `sw_daily`, `bak_basic` | verified/partial | 按供应商返回行数保存；`top_list`、`limit_list_d` 当日为合法空集 |
| Tushare Super GET/ProMax | `daily`, `daily_basic`, `moneyflow` | verified | GET + `X-API-Key` 通道实测返回；只覆盖已登记 GET 能力 |
| Tushare Super GET/ProMax | `rt_k`, `rt_min`, `rt_min_daily` | verified_partial | 仅交易时段使用；收盘后探针按门禁跳过，不能据此宣称当前实时 |
| AKShare 1.18.93 | 日线、市场总貌、龙虎榜、强势池、涨跌停池、宽度、资金流、龙虎榜席位、大宗交易、公司风险、分析师热度、宏观/商品 | completed | 2026-08-21 探针均写入独立能力健康；结果受公开源时效和分页上限约束 |
| 腾讯财经 | 批量全 A 快照、观察池盘口、日线 | completed | 全 A 5549 行；观察池盘口支持逗号批量请求 |
| 新浪财经 | 观察池批量报价 | completed | 小批量交叉报价；不作为全市场收盘主源 |
| 东方财富 | 单票报价、概念/行业资金流、涨停池 | partial | 单票报价和板块/涨停能力可用；部分日线/批量资金接口触发公开源重试失败 |
| 巨潮资讯 | 单票公告 | completed | 观察标的公告查询返回记录 |

## 明确不可宣称为成功

- 主源 Tushare 没有已验证实时能力，保持 `realtime_coverage=unavailable`。
- Super SDK 的 `st`、`stk_shock`、`stk_alert`、`dc_concept` 等本次返回上游 404/unsupported；不从目录声明推导可用。
- Super GET 不支持 Super SDK 的行业、龙虎榜、涨停梯队等扩展接口；这些继续走 Super SDK/AKShare。
- Xinhua Finance 未配置授权，状态为 `not_configured`。
- 收盘后实时路由（`rt_k`/`rt_min`/`rt_min_daily`）因交易时段门禁跳过，空集不是失败。
- 当日 Tushare 龙虎榜 `top_list` 返回合法空集；前端 LHB 今日为 0 条，不补造数据。

## 盘后流水线状态

- 收盘复盘：已落库，前端 `/api/research/strategy/reviews/latest?session=close` 返回 2026-08-21。
- 板块资金曲线：前端返回 91 条行业曲线。
- 涨停/连板模式挖掘：`completed`，分钟样本 20 条，精选 10 条；涨停池为同花顺与东财去重并集，不等同交易所官方全量。
- 基础蓄势候选：仍 `blocked`，今日只保存 45 个日线标的，门禁要求至少 1000；不降低门槛、不生成伪候选。
- 一键全市场刷新：本次曾因全市场日线阶段阻塞事件循环并触发容器自动重启；已清理陈旧租约，服务现已健康。后续应将全市场同步拆到后台作业/独立 worker 后再重试。

## 运行与回归

- `python -m unittest discover -s tests -p 'test*.py'`：501 tests，全部通过。
- 量化服务 `/health`：`ok`；当前无盘后刷新租约残留。
- 前端代理关键读接口均 HTTP 200：收盘复盘、板块报告、行业曲线、盘后候选、模式挖掘、龙虎榜。

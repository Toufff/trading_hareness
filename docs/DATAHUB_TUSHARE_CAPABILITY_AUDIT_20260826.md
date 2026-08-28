# DataHub Tushare 兼容源审计（2026-08-26）

## 结论

DataHub 已存在于部署配置（`TUSHARE_BACKUP_API_URL` / `TUSHARE_BACKUP_API_KEY`），此前仅接入 `stock_basic`。本次将其扩展为一个**研究只读、显式观测白名单**的备用源：只有 `tushare_providers.py` 中 `DATAHUB_VERIFIED_APIS` 的接口会被自动路由到 DataHub；不会改变策略阈值、下单或实时确认路径。

DataHub URL 使用短横线路径：内部 `daily_basic` 对应 `/app-api/openapi/v1/tushare/daily-basic`。请求头为 `X-API-Key`，响应采用 `{code, data:{fields, items}}`，并由统一解码器转换为行对象。

## 有界接口探测

按本地 Tushare catalog 的 200 个接口执行探测。对其中 169 个有默认安全参数的接口进行了请求；31 个需要人工补充参数的接口未臆测请求。结果：

| 结果 | 数量 | 解释 |
| --- | ---: | --- |
| HTTP 200 | 129 | 返回了合法 HTTP 响应；仍需看 `code` 与字段形状 |
| `code=0` | 116 | 响应信封和参数契约通过 |
| `code=0` 且有行 | 82 | 本次样例参数有数据 |
| `code=0` 但空行 | 34 | 可能是合法空集，不代表接口不支持 |
| HTTP 502 | 18 | 混合了明确无权限和缺少必填参数，不能一概判为不支持 |
| 超时 | 22 | 未验证，不计入支持数 |

探测使用了有限日期窗口和一个样例证券代码；空集、超时和参数错误均保留为证据，不提升为“已支持”。

## 已纳入 DataHub 白名单的接口族

已验证并可作为自动路由备用的接口包括：

- 行情/复权/日历：`stock_basic`, `daily`, `daily_basic`, `adj_factor`, `trade_cal`, `index_daily`, `stk_limit`, `suspend_d`, `weekly`。
- 龙头与涨停研究：`limit_list_d`, `limit_list_ths`, `limit_step`, `limit_cpt_list`, `kpl_list`, `top_list`, `top_inst`, `report_rc`。
- 资金与筹码：`moneyflow`, `moneyflow_dc`, `moneyflow_ths`, `moneyflow_ind_dc`, `moneyflow_ind_ths`, `moneyflow_mkt_dc`, `moneyflow_cnt_ths`, `moneyflow_hsgt`, `cyq_perf`, `cyq_chips`, `stk_factor_pro`。
- 同花顺/概念：`ths_index`, `ths_daily`, `ths_member`, `ths_hot`, `dc_hot`。
- 财务与公告：`forecast`, `express`, `income`, `cashflow`, `fina_mainbz`, `fina_mainbz_vip`, `disclosure_date`, `dividend`, `namechange`, `new_share`, `share_float`, `stk_holdernumber`, `stock_st`。
- 基金/期货/海外/宏观：`etf_basic`, `etf_share_size`, `etf_sz_cons`, `fund_daily`, `fund_nav`, `fund_share`, `fut_basic`, `fut_daily`, `fut_settle`, `fut_trade_cal`, `fut_wsr`, `ggt_daily`, `ggt_top10`, `hibor`, `shibor`, `shibor_quote`, `sge_daily`, `us_tbr`, `us_tltr`, `us_trltr`, `us_trycr`, `us_tycr`, `cn_cpi`, `cn_m`, `cn_pmi`, `cn_ppi`, `eco_cal`。

实时 `rt_k` / `rt_min`、历史分钟 `stk_mins` 等未纳入 DataHub 白名单：即使单次请求返回，也没有完成时间戳、连续性和留存边界审计，不能进入盘中确认链路。

## 参数契约

统一保留调用方的 Tushare 参数，并仅对 `limit` 做 1–3000 的边界约束；常用参数如下：

| 参数 | 适用接口 | 处理规则 |
| --- | --- | --- |
| `ts_code` | 个股、指数、基金、概念成员等 | 原样传递；上层请求模型负责代码格式和必填校验 |
| `trade_date` | 日线、资金、涨停池等 | 原样传递；日期窗口由 `TushareFetchRequest` 限制 |
| `start_date` / `end_date` | 区间接口 | 原样传递；不放宽本地最大窗口 |
| `exchange` / `list_status` | `stock_basic`、交易日历等 | 原样传递；DataHub 对未知参数可能静默忽略，因此不能替代本地白名单校验 |
| `limit` / `offset` | 支持分页的列表接口 | `limit` 限制为 1–3000；分页由统一 `call_with_fallback` 控制 |
| `freq` | 分钟/周期扩展接口 | 仅在上层模型允许的枚举中传递，缺失时不猜测默认值 |

已验证的 `stock-basic` 示例：`limit=3`、`offset=3`、`exchange=SSE`、`list_status=L`、`ts_code=000001.SZ` 均返回 `code=0`；未知 `foo=bar` 会被忽略，所以本地参数验证必须继续保留。

## 运行时边界

- DataHub 默认限速为 6 次/分钟，作为自动路由的最后备用候选，不抢占主源优先级。
- 明确选择 `provider=backup` 才会强制只调用 DataHub；自动路由只在前置候选失败或返回合法空集时继续尝试。
- 所有返回仍保留 provider/source 证据；本次接入不产生任何 live effect，也不自动生成交易指令。

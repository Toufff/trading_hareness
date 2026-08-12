# 超级源旧路径模式审计（已过时）

> 本文记录 2026-08-09 早期未配置代理、且缺少 SDK 内部参数时的失败现象，不能代表当前服务。
> 当前有效结论见 [超级源 SDK 代理能力审计](TUSHARE_SUPER_SDK_CAPABILITY_AUDIT.md)。

审计日期：2026-08-09。目标是当前配置的 `tushare_super`，通过
`POST /api/v1/providers/tushare/fetch` 强制指定 `provider: "super"` 执行。
每项仅使用一个标的或一个交易日，且 `max_rows=2`；本文件不包含 Token 或地址。

## 结论

当前超级源不能作为以下专题数据的可用来源。路径式调用对多数接口返回重复的“字段名=
字段值”伪表头；服务现已拒绝这类响应、将请求记为 `failed`，并且不保存到原始证据库。
用同一地址按官方 Tushare SDK 的标准 POST 协议尝试 6 项代表接口，均返回 HTTP 404、
`接口不存在`，因此也不能将其当作标准 SDK 入口使用。

“completed, 0 行”只代表服务返回了合法空集；它不是有数据能力的证明。需由供应商提供
有效板块代码、日期和一份非空样例后，才能重新验收。

## 路径式接口矩阵

| 主题 | 接口 | 结果 |
| --- | --- | --- |
| 概念板块 | `ths_index` | 失败：重复表头 |
| 概念/板块成分 | `ths_member`、`dc_member`、`tdx_member`、`kpl_concept_cons` | 合法空集，未验证有数据能力 |
| 东财/通达信板块 | `dc_index`、`tdx_index` | 分别为 HTTP 400、参数不能为空 |
| 个股及行业/大盘资金流 | `moneyflow`、`moneyflow_ths`、`moneyflow_dc`、`moneyflow_ind_ths`、`moneyflow_ind_dc`、`moneyflow_mkt_dc` | 均失败：重复表头 |
| 筹码 | `cyq_perf`、`cyq_chips` | 均失败：重复表头 |
| 龙虎榜 | `top_list`、`top_inst` | 均失败：重复表头 |
| 量化因子 | `stk_factor`、`stk_factor_pro` | 前者已列为超级源不支持；后者失败：重复表头 |
| 盈利预测/机构调研 | `report_rc`、`stk_surv` | 合法空集，未验证有数据能力 |
| 游资 | `hm_list`、`hm_detail` | 均失败：重复表头 |
| 涨停 | `limit_list_ths`、`limit_list_d`、`limit_step`、`limit_cpt_list` | 均失败：重复表头 |
| 个股及行业热榜 | `ths_hot`、`dc_hot`、`kpl_list` | 均失败：重复表头 |

## 平台保护

- `looks_like_response_header` 会识别一行或多行的重复表头并拒绝持久化。
- 本次审计结束时，`tushare_super` 的保留伪表头记录为 0。
- 主 Tushare、备用源及公开数据源保持独立，超级源失败不会被作为成功数据进入特征、推荐或回测。

重新接入前，需要供应商明确该服务的协议、接口路由、有效参数和至少一份真实非空响应。

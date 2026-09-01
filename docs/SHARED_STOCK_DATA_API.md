# 全量共享股票数据 API 参考

> 契约版本：2026-09-01
>
> 服务：trading_hareness / Market Research Service
>
> 目标：让受信任的 peer Agent 调用原始 `接口.docx` 中的全部股票数据接口，同时不泄露 owner 的上游 Token、UserID 和 DeviceID。
> 唯一供应商约束：任何单次物理请求最多 300 条；更大的逻辑请求由网关自动拆成多次请求。

## 1. 能力范围

本接口不再按 action 白名单限制业务能力。调用方可以：

- 调用文档中全部 Longhu 历史、行情、盘口、竞价、板块、情绪、机构、股东、资讯和主题接口；
- 调用文档中的选股宝涨跌停池、市场指标、异动股票/板块接口；
- 调用文档中的复盘网盘中直播接口；
- 传入任意 action、controller 和业务参数；
- 请求 `st > 300` 时由网关自动分页；
- 对显式股票代码列表超过 300 个时由网关自动分组。

网关只固定 8 个文档内上游主机，避免任意 URL/SSRF。主机内的 action 与业务参数不受限制。

## 2. 连接与鉴权

### 2.1 lightServer 本机

```text
http://127.0.0.1:15682
```

### 2.2 其他电脑

先建立 SSH 隧道：

```bash
ssh -N -p 3535 -i ~/.ssh/stockpeer_ed25519 \
  -L 15682:127.0.0.1:15682 \
  stockpeer@47.110.79.189
```

随后访问：

```text
http://127.0.0.1:15682
```

### 2.3 长期密钥

所有受保护请求带：

```http
X-Quant-Read-Key: <QUANT_SHARED_READ_API_KEY>
```

推荐环境变量：

```dotenv
STOCK_API_BASE_URL=http://127.0.0.1:15682
QUANT_SHARED_READ_API_KEY=<private handoff value>
```

该共享密钥不自动过期、不定期轮换，只在人工撤权、疑似泄露或明确维护时更换。上游 Longhu Token、UserID 和 DeviceID 由 owner 网关自动注入，不提供给 peer。

## 3. 稳定路由

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 服务存活检查 |
| GET | `/licensed/stock-api/catalog` | 全量目标、操作和 89 个原文档调用样例 |
| POST | `/licensed/stock-api/call` | 通用全量调用与自动 300 分批 |
| GET | `/licensed/longhu/quotes` | 兼容接口：标准化批量行情 |
| GET | `/licensed/longhu/minutes/{symbol}` | 兼容接口：标准化单股分钟线 |

Swagger 与 OpenAPI：

- `http://127.0.0.1:15682/docs`
- `http://127.0.0.1:15682/openapi.json`

## 4. 目标主机

通用调用使用 `target`，不直接传 URL。

| target | 上游 | 默认 path | 内容 |
|---|---|---|---|
| `longhu_history` | `apphis.longhuvip.com` | `/w1/api/index.php` | 历史 K 线、历史情绪、历史竞价、机构持仓等 |
| `longhu_quote` | `apphwhq.longhuvip.com` | `/w1/api/index.php` | 行情、分钟、盘口、竞价、筹码、大单等 |
| `longhu_market` | `apphq.longhuvip.com` | `/w1/api/index.php` | 市场监控、排名、全球指数等 |
| `longhu_market_wide` | `apphwshhq.longhuvip.com` | `/w1/api/index.php` | 市场广度、板块、调研、偏离值等 |
| `longhu_lhb` | `applhb.longhuvip.com` | `/w1/api/index.php` | 股东、机构跟踪、主题信息 |
| `longhu_article` | `apparticle.longhuvip.com` | `/w1/api/index.php` | 资讯与快讯 |
| `xuangubao` | `flash-api.xuangubao.com.cn` | `/api/pool/detail` | 涨跌停池、市场曲线、异动 |
| `fupanwang` | `api.fupanwang.com` | `/kpl/zhibo` | 盘中直播 |

调用方不能改主机，但可为已登记目标传不同 path。path 必须以 `/` 开头，不能包含目录穿越。

## 5. 查询全量目录

### 请求

```bash
curl --noproxy '*' -fsS \
  -H "X-Quant-Read-Key: $QUANT_SHARED_READ_API_KEY" \
  "http://127.0.0.1:15682/licensed/stock-api/catalog"
```

### 返回结构

```json
{
  "targets": [],
  "documented_operations": [],
  "documented_examples": [],
  "external_paths": {},
  "physical_batch_limit": 300,
  "operation_restriction": "none_within_registered_targets"
}
```

`documented_examples` 含从原始 `接口.docx` 提取并去除敏感凭据的 89 个样例。每条包括：

| 字段 | 说明 |
|---|---|
| `id` | 稳定样例标识，可供 Agent 查找 |
| `source_index` | 原文档 URL 顺序 |
| `target` | 通用调用目标 |
| `path` | 上游 path |
| `action` | Longhu action；公共源可能为空 |
| `controller` | Longhu controller；公共源可能为空 |
| `params` | 去除 Token/UserID/DeviceID 后的完整示例参数 |

Agent 应先读取 catalog，再从最接近需求的样例复制参数并替换股票、日期、板块等业务值。

## 6. 通用调用

### 6.1 请求结构

```http
POST /licensed/stock-api/call
Content-Type: application/json
X-Quant-Read-Key: <key>
```

```json
{
  "target": "longhu_history",
  "path": "/w1/api/index.php",
  "params": {
    "a": "GetKLineDay_W14",
    "c": "StockLineData",
    "apiv": "w40",
    "StockID": "600664",
    "Type": "d",
    "Is_FS": "1",
    "st": 800,
    "Index": 0
  },
  "batch": null
}
```

### 6.2 字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `target` | 是 | 第 4 节目标之一 |
| `path` | 否 | 省略时使用目标默认 path |
| `params` | 否 | 原上游 query 参数；action/controller 不限制 |
| `batch` | 否 | 对一个参数的一组显式值自动按 300 分组 |

`Token`、`UserID`、`DeviceID` 即使由调用方传入也会被移除，并替换为 owner 凭据。其他参数原样透传，包括 `a`、`c`、`apiv`、`VerSion`、`Type`、`Date`、`Day`、`StockID`、`PlateID` 等。

### 6.3 返回结构

```json
{
  "target": "longhu_history",
  "path": "/w1/api/index.php",
  "calls": 3,
  "batched": true,
  "physical_batch_limit": 300,
  "requested_size": 650,
  "batch_param": null,
  "batch_value_count": null,
  "pages": [
    {
      "offset": 0,
      "size": 300,
      "batch_count": null,
      "payload": {}
    }
  ]
}
```

`payload` 是上游原始 JSON，不做字段删减。不同 action 的结构不同，调用方按 catalog 样例和实际返回解析。

## 7. 自动 300 分页

若 `params.st <= 300`，只发一次物理请求。

若 `params.st > 300`，网关自动拆分。例如：

```json
{"st": 650, "Index": 7}
```

会转换成：

1. `st=300, Index=7`
2. `st=300, Index=307`
3. `st=50, Index=607`

若原参数使用小写 `index`，网关保持小写；否则使用 `Index`。

服务不会因为总量大于 300 而拒绝逻辑请求，只保证每次上游物理调用不超过 300。

## 8. 显式值列表分批

对于需要把大量股票代码放入同一 query 参数的接口，使用 `batch`：

```json
{
  "target": "longhu_quote",
  "params": {
    "a": "SomeDocumentedAction",
    "c": "StockL2Data"
  },
  "batch": {
    "param": "StockIDs",
    "values": ["600000", "600001", "..."],
    "separator": ","
  }
}
```

650 个值会拆成 300、300、50 三次调用。这里要求对应上游接口本身支持列表参数；对于只接受单个 `StockID` 的接口，应逐只调用。若同时 `st=650`，则两种分批做笛卡尔组合，共 3 × 3 = 9 次物理调用。

## 9. curl 示例

### 9.1 个股盘口

```bash
curl --noproxy '*' -fsS \
  -H "X-Quant-Read-Key: $QUANT_SHARED_READ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "longhu_quote",
    "params": {
      "a": "GetStockPanKou",
      "c": "StockL2Data",
      "apiv": "w41",
      "StockID": "600664"
    }
  }' \
  http://127.0.0.1:15682/licensed/stock-api/call
```

### 9.2 301 条历史日线，自动 300+1

```bash
curl --noproxy '*' -fsS \
  -H "X-Quant-Read-Key: $QUANT_SHARED_READ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "longhu_history",
    "params": {
      "a": "GetKLineDay_W14",
      "c": "StockLineData",
      "apiv": "w40",
      "StockID": "600664",
      "Type": "d",
      "Is_FS": "1",
      "st": 301,
      "Index": 0
    }
  }' \
  http://127.0.0.1:15682/licensed/stock-api/call
```

### 9.3 选股宝涨停池

```bash
curl --noproxy '*' -fsS \
  -H "X-Quant-Read-Key: $QUANT_SHARED_READ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "xuangubao",
    "path": "/api/pool/detail",
    "params": {"pool_name": "limit_up"}
  }' \
  http://127.0.0.1:15682/licensed/stock-api/call
```

## 10. Python Agent 客户端

```python
from __future__ import annotations

import os
from typing import Any

import requests

BASE_URL = os.getenv("STOCK_API_BASE_URL", "http://127.0.0.1:15682").rstrip("/")
READ_KEY = os.environ["QUANT_SHARED_READ_API_KEY"]

session = requests.Session()
session.trust_env = False
session.headers.update({
    "X-Quant-Read-Key": READ_KEY,
    "Accept": "application/json",
})


def catalog() -> dict[str, Any]:
    response = session.get(
        f"{BASE_URL}/licensed/stock-api/catalog",
        timeout=(5, 30),
    )
    response.raise_for_status()
    return response.json()


def call(
    target: str,
    params: dict[str, Any],
    *,
    path: str | None = None,
    batch_param: str | None = None,
    batch_values: list[Any] | None = None,
    separator: str = ",",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "target": target,
        "params": params,
    }
    if path is not None:
        body["path"] = path
    if batch_param is not None:
        body["batch"] = {
            "param": batch_param,
            "values": batch_values or [],
            "separator": separator,
        }

    response = session.post(
        f"{BASE_URL}/licensed/stock-api/call",
        json=body,
        timeout=(5, 600),
    )
    response.raise_for_status()
    return response.json()


def call_example(example_id: str, **overrides: Any) -> dict[str, Any]:
    examples = catalog()["documented_examples"]
    example = next(row for row in examples if row["id"] == example_id)
    params = {**example["params"], **overrides}
    return call(
        example["target"],
        params,
        path=example["path"],
    )


if __name__ == "__main__":
    result = call(
        "longhu_quote",
        {
            "a": "GetStockPanKou",
            "c": "StockL2Data",
            "apiv": "w41",
            "StockID": "600664",
        },
    )
    print({
        "calls": result["calls"],
        "batched": result["batched"],
        "payload_type": type(result["pages"][0]["payload"]).__name__,
    })
```

## 11. 全部 Longhu action

下列 action 均可通过 `POST /licensed/stock-api/call` 调用；参数模板见 catalog 的 89 个样例。

### 行情、分钟、盘口、竞价、筹码

- `GetKLineDay_W14`
- `GetStockTrendIncremental`
- `GetStockPanKou`
- `GetMainMonitor_w30`
- `GetWeiTuo_W14`
- `MorningBiddingList`
- `GetStockBid`
- `StockChouMaByTimeNew_W5`
- `GetStockChouMa_New`
- `GetStockDaDanTrendIncremental`
- `GetZhangTingGene`
- `GetBKJJ_W36`
- `GetBKJJBL`

### 市场、板块、情绪、涨跌停

- `GetPlateInfo_w38`
- `RiseFallAnalysis`
- `MoodNumCount`
- `GetPlate_Info_QJ`
- `ChangeStatistics`
- `RealRankingInfo`
- `ZhiShuStockList_W8`
- `DailyLimitPerformance`
- `DailyLimitPerformance2`
- `GroupCount_w28`
- `Radar`
- `GetHotPHB`
- `GlobalCommon`
- `GetPMSL_KQXY`
- `GetPianLiZhi_Index`

### 调研、机构、股东、资讯、主题

- `GetInterviewsByDateZS`
- `GetInterviewsByDateStock`
- `GGList_JGCC`
- `GGList_JGCC_Plate_Stocks`
- `GGList_BXZJ`
- `GGList_BXZJ_Stocks`
- `GuDongRenShu`
- `JGStockListox`
- `GetJGNameID`
- `GetStockIDPlate`
- `GetTopList`
- `GetList`
- `InfoList`
- `InfoZS`
- `InfoGet`
- `ZhiBoContent`

catalog 不限制未来新增或原文档遗漏的 action：只要属于已登记 target 主机，调用方仍可通过通用接口传入。

## 12. 外部公共接口路径

### 选股宝

- `/api/pool/detail`
  - `pool_name=limit_up`
  - `pool_name=limit_up_broken`
  - `pool_name=limit_down`
  - `pool_name=yesterday_limit_up`
- `/api/market_indicator/line`
  - `rise_count,fall_count`
  - `limit_up_count,limit_down_count`
  - `limit_up_broken_count,limit_up_broken_ratio`
  - `yesterday_limit_up_avg_pcp`
  - `market_temperature`
- `/api/surge_stock/stocks`
- `/api/surge_stock/plates`

### 复盘网

- `/kpl/zhibo`

## 13. 兼容的标准化接口

原来的两个读取接口继续保留，适合不想解析原始 payload 的 Agent：

### 批量行情

```http
GET /licensed/longhu/quotes?symbols=600664.SH,600487.SH
```

单个兼容请求仍最多 300 只；若需要更多，使用通用接口的 `batch`，或由客户端分组。

标准字段包括：

- `ts_code`、`name`、`price`、`pre_close`
- `open`、`high`、`low`、`pct_change`
- `volume`、`amount`、`turnover_rate`、`volume_ratio`
- `amplitude`、`pe_ttm`、`pb`
- `trade_date`、`trade_time`

### 单股分钟

```http
GET /licensed/longhu/minutes/600664.SH
```

标准字段包括：

- `symbol`、`time`、`close`、`vwap`
- `volume_lot`、`amount`、`cumulative_volume_lot`
- `cumulative_segment`、`is_complete`、`source`

## 14. 错误处理

| 状态 | 原因 | 处理 |
|---:|---|---|
| 200 | 网关调用完成 | 仍需检查每个 `pages[].payload` 的上游错误字段 |
| 401 | 共享读取密钥错误 | 不重试，修正密钥 |
| 422 | target/path/请求结构错误 | 修正参数 |
| 503 | provider 或共享网关未配置 | 检查部署和环境 |
| 5xx | 上游超时、非 JSON、网络或内部错误 | 指数退避重试失败的逻辑请求 |

通用代理不会伪造统一业务成功字段。Longhu 不同 action 可能在 HTTP 200 中返回自己的 `errcode`；Agent 必须按原始 payload 判断业务成功。

## 15. 数据纪律

- 原始 payload 必须连同 `target`、path、业务参数、调用时间和页偏移一起落库，才能复现。
- 分页结果不能只保存第一页。
- 不同 action 中同名字段未必同义，未经核查不得强行合并。
- `GetStockPanKou` 的交易时间优先于 HTTP 接收时间。
- 分钟接口最后一条可能未完成。
- 大单、主力、筹码、异动等均是供应商口径，不能自动等同于机构身份或真实买卖意图。
- 通用接口只读取股票数据，不含券商下单、撤单和转账能力。

## 16. 实际验收标准

只有以下全部成立才可声称接入成功：

1. SSH 隧道可用。
2. `/health` 返回 200。
3. catalog 返回 8 个目标和 89 个样例。
4. 无密钥调用受保护接口返回 401。
5. `GetStockPanKou` 返回非空 dict。
6. `RiseFallAnalysis` 返回非空 dict。
7. 选股宝涨停池返回非空 dict。
8. `GetKLineDay_W14 st=301` 返回 `calls=2`，两次物理大小为 300 和 1。
9. 返回内容不包含 owner Token、UserID 或 DeviceID。
10. peer 端通过 15682 调用成功，而不只是 owner 本机直调成功。

本契约在 2026-09-01 已按 catalog 中的 89 个原文档请求模板逐一发起真实调用；89 个模板均得到 JSON 响应。该结果验证接口可达性，不代表所有历史日期或股票参数在未来都必然返回非空业务数据。

peer 主机可随时复跑端到端验收：

```bash
python3 scripts/shared-peer/verify-complete-stock-api.py
```

脚本会实际检查：无密钥返回 401、目录为 8 个目标/89 个样例、盘口/市场广度/公共源可用、301 条历史请求拆成 300+1，以及响应不含 owner 凭据字段。

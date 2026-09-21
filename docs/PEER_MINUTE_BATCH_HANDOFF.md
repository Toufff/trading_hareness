# 协作者分钟线批量接口交接

## 范围和职责

本次只在 Windows owner 增加只读数据接口，对齐协作者提交
`49ecf02e44d3588340afe22fc9b0a6581a01669c` 的分钟线协议。
没有整合协作者的 main、教师审查、盘后结算、策略或飞书通知；它们继续由协作者负责。
没有新增数据库迁移、环境变量、SSH 登录权限或上游凭据分享；owner 的共享池仍默认 4 worker / 8 queue。
不需要为本次接口部署设置 TEACHER_REVIEW_ENABLED，因为没有引入该模块。

已有批量报价、通用股票 API、单股分钟线均保留。新增接口是减少跨隧道往返的多股票聚合层，
不是给供应商单个 StockID 参数拼接多只股票，也不是批量数据库隧道。

## 地址、鉴权和请求

- 47 主机 shell：`http://127.0.0.1:15681`。
- 现有 peer 容器：`http://db-tunnel:5681`，沿用 QUANT_SHARED_READ_API_BASE_URL。
- 鉴权：现有 `X-Quant-Read-Key`。不得把 key 放进 URL、日志或文档。

```bash
curl --fail-with-body --compressed --max-time 10 \
  -H "X-Quant-Read-Key: $QUANT_SHARED_READ_API_KEY" \
  'http://127.0.0.1:15681/licensed/longhu/minutes?symbols=600519.SH,000001.SZ&deadline_seconds=4'
```

`GET /licensed/longhu/minutes`：

| 参数 | 约定 |
|---|---|
| symbols | 必填，逗号分隔，6 位代码加 .SH/.SZ/.BJ；去首尾空白、大写化、去重，1–300 个唯一代码；字符串最多 4,000 字符 |
| deadline_seconds | 可选，默认 5.5 秒，有限数值 1–20 秒；包含 owner 阻塞池等待和数据抓取，不包含隧道传输/序列化 |

超过 300 个标的请分篮串行调用。不是报价接口的无限逻辑分页规则；报价原有 >300 分页不变。

## 返回契约

示例仅说明结构，不代表真实报价：

```json
{
  "rows": {"600519.SH": [{"symbol": "600519.SH", "session_date": "2026-09-22", "trade_date": "20260922", "trade_time": "2026-09-22 09:31:00", "time": "0931", "close": 100.0, "volume_lot": 10, "amount": 100000, "is_complete": false}]},
  "errors": {"000001.SZ": "minute_batch_deadline_exceeded"},
  "requested": 2, "completed": 1, "deadline_seconds": 4.0,
  "session_guard": "current_exchange_session",
  "source": "longhuvip:GetStockTrendIncremental", "physical_request_limit": 300
}
```

- `rows` 是以规范股票代码为键的字典，值为分钟行列表；不是单股接口的扁平列表。
- `errors` 是逐股错误字典，每个请求的代码恰好进入 rows 或 errors，不混入其他股票。
- `requested` 为去重后代码数；`completed` 为成功股票数，不是分钟数。
- HTTP 200 可包含部分失败或全部失败，必须检查 completed/errors；不得把空值当成功。
- 保留 owner 现有分钟字段和单位：volume_lot/vol 为手、amount 为人民币元；VWAP 推导金额、缺失值、最后一分钟未完成标记不变。
- 为兼容协作者加 trade_date/trade_time 别名，不覆盖 session_date，不更换 owner 既有金额计算。
- `physical_request_limit=300` 是兼容字段/篮子上限，不表示龙虎一次原生调用取得 300 只分钟线。

### 会话日期和错误

批量接口只接受响应返回时 **Asia/Shanghai 当天日期** 的分钟线。
这与 49ecf02 的 current_exchange_session 约定一致，不自动回退到上一交易日。
凌晨、周末或开盘前，供应商仍返回旧交易日时，HTTP 200 + 逐股 stale 错误是正确结果。
不能改日期、填假数据或拿旧分钟线参与当日教师信号。单股旧接口仍返回供应商最新会话，调用方自行核对日期。

| errors 值 | 含义/动作 |
|---|---|
| minute_batch_deadline_exceeded | 到截止时尚未取得可用结果；减少篮子或在预算范围内延长，稍后重试 |
| minute_batch_stale_or_invalid_rows | 空、缺日期、混日期、旧日期或结构不合格；不得参与当日决策 |
| minute_batch_invalid_symbol | 代码无法解析；校正后再请求 |
| minute_batch_fetch_failed:异常类名 | 上游/解析异常；不回显可能含 Token 的异常正文 |
| minute_batch_missing_symbol | 防御性返回：服务没有给该代码结果，不应当作空的成功列表 |

HTTP 401 为鉴权失败；422 为请求格式/数量/截止时间不合法；503 为网关关闭或容量忙；
容量忙带 Retry-After: 1；504 为外层执行截止；502 为批量提供者初始化/执行不可用。
503/502/504 不要立即回退成数百次单股调用，应有界退避并记录失败。

## 并发与压缩

整批只提交一次既有阻塞池。内部每个 worker 使用自己的供应商连接，最多
min(供应商 workers, 12) 路；不提高 AKSHARE_MAX_WORKERS/AKSHARE_MAX_QUEUE。
**每个 API 进程仅允许一个活跃分钟篮子，包括超时后尚未结束的上游请求。**
后来的篮子收到 503；截止后不再为该篮子启动新股票请求。HTTP 请求不能安全强杀，
所以它们真正结束前保留批量占位，避免重试形成无限私有线程池。
没有新增缓存、定时任务、数据库写入或飞书发送。

响应至少 64 KiB 且 Accept-Encoding 接受 gzip 时压缩；gzip;q=0 不压缩。
响应包含 Vary: Accept-Encoding 和 Cache-Control: no-store。requests/curl --compressed 可自动解压。

## 协作者客户端兼容

49ecf02 的 SharedLonghuReadSource.stock_minutes_batch 会优先调用本路径，
owner deadline = 客户端预算 −1.5 秒（限制在 1–20）。仅 404/405 才暂时回退逐股请求，
并等待 600 秒重新探测；503 等错误不会触发逐股洪泛。
因此部署后该客户端最迟在负缓存到期后的下一次调用使用新路由，不需要 owner SSH。

重要：47 上运行中的旧容器和 hotfix-src 仓库不是同一个版本。
本次不替协作者升级/重启其服务；如仍不切换，先确认实际调用进程是否已包含
stock_minutes_batch/_gateway_minutes_batch，而不只是磁盘上存在 49ecf02。

## 验收、发布与回退

源码测试覆盖会话日期、午夜边界、逐股失败、错误脱敏、截止、线程占位、并发拒绝、
鉴权、参数、去重、旧接口和 650 股票报价兼容、gzip、OpenAPI。
上线必须由 47 主机用既有 key 验证单股/批量/通用路径，检查 rows/errors 和会话日期；
仅 curl HTTP 200 不是数据质量验收。

代码：app/longhu_minute_batch.py、app/routers/longhu_reads.py；main.py 只有导入和依赖接线。
执行标准 owner 发布，保留当前已上线工作。回退使用 switch-stock-release.ps1 切回保留版本，
不改 release 文件、不需要数据库恢复。回退后的 404 会让兼容客户端使用旧路径。

本轮部署版本、测试统计和真实读回在完成后补充；不得提前视为生产验收已完成。

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

### 2026-09-22 上线回执

- owner release：`20260922T020134-964b19d10abc-clean`。
- 源码：`964b19d10abca58c22d15531b030bd05846485b8`，干净提交，保留先前正式版本 9e917df 的 Git 祖先。
- 前一版：`20260922T012644-9e917df1238b-clean`，仍保留可回退。
- 标准发布门禁：后端 **3222 passed / 130 skipped / 890 subtests**；前端 **141 tests / 34 files**；typecheck/build 通过。
- 相关专项：**76 passed / 10 subtests**，包括本次新增的 32 个测试；跳过项不冒充已验收。
- 在线 OpenAPI **196 paths** 与 generated.ts 一致，在线 api:check 通过。
- 共享隧道 `reused_without_reinstall`，未重建 peer 服务；02:06:22 shared-runtime verified。
- owner 本地与 47 容器：缺 key/错 key 均 401，超出截止范围/301 只均 422，去重后两只批量 200；旧单股、两股报价、通用 catalog 均正常。
- 47 实际调用 49ecf02 原版 `_gateway_minutes_batch` 方法成功，大小写键映射和逐股错误解析通过；未修改该方法，也未改运行中服务。
- 凌晨真实供应商会话为 **2026-09-21**，单股各 241 行；批量返回 completed=0、逐股 `minute_batch_stale_or_invalid_rows`，证明没有错标今天。
- **尚未实测边界**：本轮未等到 9 月 22 日开盘，真实当日成功行和大篮子 gzip/延迟在生产尚未观测；成功行、gzip、部分失败、取消和超时容量保护已有自动化测试，不混称盘中实测。
- 两个现有 peer 容器内均未发现 stock_minutes_batch/_gateway_minutes_batch；请协作者确认自己的实际调用进程版本。仅更新 hotfix-src 仓库不会让旧容器自动切换。

标准发布保留 6 份构建，清理旧构建 `20260921T175408-e6a120ed18c4-clean`（可由 Git 重建）；
被占用的另一旧目录延后清理，未强杀或强删。前端存在既有 >500 kB chunk 告警，不影响本接口。

完整脱敏证据在 owner `G:/StockPlatform/data/research/peer-minute-batch-20260922`，包含本机/peer/原版客户端
JSON 回执、发布日志、测试日志、manifest、验收脚本和本文。peer 文档稳定位置：
`/home/stockpeer/owner-api-handoff/PEER_MINUTE_BATCH_HANDOFF.md`。

## 可直接转交给协作者的回复

> owner 已单独上线与你 49ecf02 兼容的 GET /licensed/longhu/minutes?symbols=...，无需 owner SSH。
> 请求参数、rows/errors、截止时间和 gzip 保持协议兼容；每个 owner 进程只容纳一个活跃篮子，
> 包含截止后尚未结束的物理请求，503 请退避，不要回退成单股洪泛。
> 没有合入教师策略、盘后结算或飞书逻辑，也没有变更 owner 线程池、环境或数据库。
> 47 上已实测 200，并使用你的原版客户端方法解析成功；凌晨旧会话正确进入 errors。
> 请确认实际运行进程已包含批量客户端；目前检查的两个旧容器没有该方法。
> 已含该方法的客户端会在 404/405 负缓存到期后的下一次调用使用新路由，最长缓存 10 分钟；
> 未含方法的旧服务需由你按自己的发布流程更新。开盘后请检查 completed/会话日期，不只看 HTTP 200。

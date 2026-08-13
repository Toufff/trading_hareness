# P1 工程加固退出审计

更新时间：2026-08-14。此文档是 P1 当前可验证状态的交接清单，不把 P2 的历史回填、分钟回放或策略阈值校准提前宣称为完成。

## 已验证的运行边界

| 边界 | 当前实现 | 证据 |
| --- | --- | --- |
| 交易数据范围 | 本轮没有新增历史数据请求；在线 Tushare 保持 allow-list、范围与行数上限，历史分钟仅允许本地文件导入 | 控制面/测试均未调用历史端点 |
| async 数据库 I/O | async 函数自身不得直接开同步事务；同步闭包经有界数据库执行器运行 | `tests/test_async_database_boundaries.py` AST 门禁；`/metrics` 显示 executor 水位 |
| 多副本循环 | 八个常驻循环各持有 `background_loop:*` 租约；失租即停、异常退出可过期接管 | `/health.runtime_leases.background_loops`、真实 `updated_at` 续租核验、租约异常测试 |
| 上游韧性 | public HTTP、飞书和非 Super-GET Tushare 均使用生命周期 keep-alive 池、受限重试与 capability 熔断；Super GET 维持独立代理会话；上游有效 `Retry-After` 只会在原有一次重试内、最多延迟 10 秒。Tushare 另以数据库原子槽位协调多副本节流，超出 5 秒（可配置）共享等待预算时明确为本地背压；等待与拒绝可由 Prometheus 逐 provider 观测；公开源错误不携带凭据入库 | provider helper 回归、`/health.http_clients`、`/health.provider_rate_limits`、`provider_health` 和 Prometheus provider 指标 |
| HTTP 调用所有权 | 策略、router、仓储与读模型不得绕过生命周期 HTTP 池；公共源、Tushare 与飞书各走独立受控 client | 静态 transport-ownership 回归，207 项全量测试 |
| Prometheus 控制面 | `/metrics` 自行以 5 秒 TTL 刷新本地连接池水位和当前 provider 熔断数；数据库异常不使 scrape 失败 | metrics-control-plane 回归，208 项全量测试 |
| 盘中扫描 | 腾讯失败、空池、闭市门禁、正常完成均写可审计终态；无观察池命中时不再伪报 Tencent `completed`；板块上下文、纸面持仓和组合快照按扫描批量读取 | `quant_intraday_scan_duration_seconds`；闭市真实调用为 `blocked` |
| 调度收敛 | n8n 18:50 日流水线只有服务端 `/pipeline/daily` 一个入口；端点内已执行特征、结算、评分与推荐，避免工作流后续节点重复计算 | `scripts/converge-n8n-quant-daily-workflow.sh` 的工作流导出回滚副本、受限更新与数据库拓扑复核 |
| 飞书投递 | 个股信号与一分钟板块轮动均先落独立 outbox；失败有界重试，成功才进入冷却；日终摘要独立持久化；连续三次失败本地留痕，首次恢复正常投递会发送运维回执；可重建的板块轮动事件/回执 60 天后有界清理 | alert delivery、板块 rotation outbox、恢复回执与日终摘要测试；服务状态页 |
| 数据传输 | 巨潮公告、来源页与附件 URL 为 HTTPS；不下载附件或远端分析师媒体 | `test_cninfo_announcement_transport_is_https_only` |
| 可恢复性 | Alembic 迁移受 advisory lock 保护；开盘预检会重建 PostgreSQL archive listing 并比对 manifest，校验备份权限和每份 workflow JSON；不执行 restore | `scripts/quant-opening-preflight.sh`、真实 12 workflow 验收与 `P1_RUNTIME_HARDENING_STATUS.md` |
| 备份原子发布 | 只有 archive manifest 与 workflow 导出完成且每份 JSON 含 `nodes` 后才发布；失败 staging 自动清理 | `scripts/backup-postgres-and-workflows.sh` 语法与 12 workflow 等价验证 |
| 回放准入 | `/api/v1/data-readiness/replay` 只读取本地日线、离线分钟与确认信号证据，并将 P2/P3 缺口显式展示给前端 | 2026-08-11 实测 `blocked`；不触发历史拉取、阈值校准或行情请求 |

## 已拆出的稳定边界

- `app/routers/`：provider/市场/盘中/板块写入操作、provider 状态、研究就绪度、盘中状态、分析师文本、公告/龙虎榜和策略结果读取。盘中与板块 router 不拥有行情 client 或调度器，仍通过显式服务依赖保留时段、限流、熔断、精确成员与 outbox 边界。
- 收盘复盘循环和日流水线的同步本地结算/特征快照/推荐生成已显式交给有界数据库执行器；AST 门禁同时阻止这些已知同步仓储函数在 async 服务中绕开该边界。
- 所有常规业务 HTTP 路由现位于 `app/routers/`：研究治理、策略、行情导入、市场、盘中、板块与 provider 均使用显式依赖装配。`main.py` 只保留健康/指标和默认关闭的 legacy schema bootstrap 控制面，便于后续以仓储和服务层继续渐进拆分。
- `app/post_close_structures.py` 提供无 I/O 的盘后结构规则，供现有盘后服务、未来 P2 回放和 P3 验证共同调用；其 30 日完整门槛和 15 日仅观察的语义未改变。
- `app/intraday_signal_policy.py` 提供无 I/O 的盘中确认、去重和冷却规则；实时扫描与未来回放共享同一纯函数契约，历史事件已完成 episode 外键修复。
- `app/daily_bar_repository.py` 承担日线 raw→canonical 选择、控制面字段保护与质量冲突记录；它仅接收事务，不能创建 HTTP/provider 客户端。主服务的 `upsert_bar` 仍是兼容入口，故现有同步、离线导入与真实 SQL 回归不改变。
- `app/public_market_repository.py` 承担公开 quote/raw evidence/公告事件的本地 SQL 读写；它没有 router、HTTP 或 provider client 依赖。主服务保留兼容函数，故公开 URL、单事务边界和既有调用顺序不变。受限研究请求超时会显式记录为 `blocked/caller_cancelled`，不会污染 provider health 或 capability 矩阵。
- 同步入口的默认股票池解析已有 `resolve_sync_symbols_async` 边界：任何 async 日线同步在未指定 symbols 时均经有界数据库执行器读取本地 core/分析师证据，而非阻塞事件循环。
- 观察池因子、策略的已持久化事件/龙虎榜/源健康上下文、单股窗口就绪度和启动期目录登记，也都经过同一有界数据库边界；盘中单事务内的日因子/分钟同比量能查询复用既有连接，避免递归新建连接。
- 受控日线同步在每个上游响应内批量写入 raw 与 canonical 日线，不再对该响应的每根 K 线开独立数据库事务；单标的失败和 provider 台账终态仍保持原语义。
- BaoStock 也先执行行级校验、再单事务批写有效日线；持久化事务失败会以明确的批失败进入 fetch ledger，不伪装为部分成功。
- Tushare 限频的环境变量是实际 limiter 的唯一运行时权威；启动阶段仅将有效数值和来源标记镜像到控制面，前端不会再把陈旧数据库数值误呈现为当前限额。
- 全局 AST 门禁锁定 `main.py` 的路由边界：除健康、指标与默认关闭的 legacy bootstrap 外，任何业务 URL 必须由 `app/routers` 的显式依赖装配提供。
- `app/*_read_model.py`：上述读取均只查本地已经保存的证据，不因前端刷新产生外部流量。
- `app/intraday_runtime_status.py`：运行状态面板的有界 SQL 证据查询。
- `app/runtime_tasks.py`、`app/runtime_leases.py`：监督、租约和异常恢复的无业务策略边界。

## 明确未完成且不在本轮启动

1. **P2 数据地基**：3–5 年日线/控制面回填、退市点时成员、历史分钟回放和指数成员；均需要独立的数据预算与验收，当前不拉取。
2. **P3 策略验证**：T+1/涨跌停撮合修正、walk-forward、DSR、盘中阈值重校准；没有 P2 回放样本不得做结论或调参。
3. **P4 组合层**：纸面账户、人工接受、T+1/费用/涨跌停与停牌门禁已落地；组合级回撤熔断、仓位映射和策略健康度自动降级仍待 P3 样本门禁通过后启用。

分析师同步专项已不再属于本节待办：n8n 现为单一无 Code 节点调度器，quant-service
负责服务侧差量、游标、共享请求间隔和 `Retry-After` 有界重试；真实 Bearer 触发已
返回 200，报告与消息游标均成功推进，连续 10 次消息空增量也返回 200 且无 429。历史执行表
中的旧 Code-node `error` 记录保留作审计，不代表当前同步服务失败。

## P1 余项的安全推进方式

- 旧 psycopg read/write 仓储仍可逐步替换为原生 async repository，但不得机械全量改写；每一条迁移须保留现有事务和限流语义，并通过现有 AST 门禁。
- `main.py` 可继续按 read model、write service、router 三层拆分；不改变公开 URL、响应结构或策略计算时序。
- provider 专项路径新增时必须同时落入：参数 allow-list、有限重试、限频、熔断、脱敏健康记录和能力矩阵，不能因“已配置 key”绕过实测治理。
- P2 的历史数据尚未得到拉取授权；在获得明确范围和存储预算前，只能使用回放准入端点展示缺口，不能将短期在线证据升级为策略验证结论。
- 新增迁移后应立即生成一次可恢复的本地归档；最近一次 `20260812-071013-daily` 已包含 revision `20260811_0008` 与共享限流表，并已验证恢复目录、权限和全部 workflow JSON。运行时如健康页缺少任一背景 lease，必须先恢复/验证续租再宣称开盘就绪。

## 下一阶段准入检查

开始 P2 前至少确认：本文件的测试/健康检查保持通过、备份最近一次可列目录校验成功、当前服务没有 degraded 的 required decision path，并由用户明确授权历史数据范围与存储预算。

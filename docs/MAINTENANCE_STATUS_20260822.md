# 平台维护迭代状态

## 已完成

- 分析师日报/周报持久化、收盘/周五调度、描述性回归和前端图表。
- `quant.automation_runs` 通用任务运行账本，支持幂等运行键、状态、输入/输出摘要和失败原因。
- `/api/research/agent/context` 无密钥上下文入口，提供模块边界、时间口径、证据流、验证命令和安全边界。
- `/api/research/automation/runs` 只读运行状态入口。
- 分析师/自动化路由拆分；写入鉴权纯函数从 `main.py` 移到 `app/security.py`。
- 后端、适配器、前端均有单独验证，并有 `scripts/verify-platform.sh` 统一执行。
- `frontend/src/api/analyst-contract.ts` 已建立独立 API 类型边界；运行中的 OpenAPI 通过 `scripts/verify-api-contract.mjs` 校验。
- 已加入 `openapi-typescript` 生成链：`frontend/src/api/generated.ts` 由运行中的 `/openapi.json` 生成，`npm run api:check` 会阻止过期契约进入验证流程。
- 盘后策略和 watchlist main-wave 调度已复用 `automation_runs`，不会只依赖各自结果表判断任务是否曾失败。
- 因子评估入口也写入同一运行账本；远端分析师同步继续使用更细粒度的 `analyst_sync_attempts` 专用证据表。
- 盘后一键刷新现在可通过编排器的 `record_stage` 回调为每个阶段写入运行回执；阶段失败与超时仍由原编排器处理，不改变重试和租约语义。
- 日终策略摘要查询已迁移到 `daily_strategy_summary_service.py`，`main.py` 仅保留兼容组合包装。
- 远端分析师消息/报告增量同步现在也写入 `automation_runs`；原有 `analyst_sync_attempts` 继续保留，用于保存逐流、逐次导入的细粒度证据。同步仍只拉取文本，不请求图片或视频。
- 策略决策快照编排已迁移到 `strategy_decision_service.py`，主文件只负责注入数据库、报告和证据读取依赖；原有路由与测试入口保持兼容。
- 午盘/收盘策略复盘投影已迁移到 `strategy_review_service.py`；它只消费已落库证据，兼容入口不再承载复盘 SQL 组合逻辑。
- 前端新增统一自动化任务账本卡片，读取 `/api/research/automation/runs` 展示任务状态、幂等键、时间和错误，不把运行状态误当成策略晋级结果。
- 增加真实 PostgreSQL 契约测试：`upsert_daily_bar` 的控制字段保护、`automation_runs` 幂等重试/失败状态、分析师证据和 episode 关系均在 compose 数据库中验证。
- 增加被动式网络韧性边界：真实外呼会记录 `unknown/degraded/offline/recovering/online` 状态；区分网络传输失败与 403/参数等 provider 错误，不做额外探测、不消耗供应商配额。后台监督循环在持续失败时指数退避（上限 60 秒），网络恢复后沿用原 `run_key`/游标继续，健康页和前端数据源 Doctor 展示最近来源、失败次数、恢复次数和脱敏错误。
- 板块盘中报告已拆为“外部快照编排”和“数据库成员精确 join”两个模块；外部失败 fail-closed，数据库 join 通过独立契约测试验证，并保留 `main.py` 兼容入口。
- 共享全 A 快照增加生命周期取消接口；盘中超时后允许有限缓存任务完成供下一轮复用，但服务关闭时会显式取消 in-flight 任务，避免 detached task 越过 HTTP/线程池关闭边界。BaoStock 旧兼容函数也已改为隔离模块转发。
- 网络状态同时暴露 Prometheus 指标：`quant_network_reachability`（状态 one-hot）、连续失败数和恢复计数；监控抓取只读本地状态，不主动探测外网。
- 小杰分析师链路已登记：远端 `POST /api/v1/imports/analysts` 已按 OpenAPI 契约创建名称为“小杰”的幂等注册任务（请求 ID 由远端返回，当前状态 `queued`，等待远端 Worker 完成后才会出现在公开分析师目录）。本地 `source-registry.json` 已注册 `#xiaojie` → `remote_analyst_id=xiaojie`，Feishu 群监听源为“小杰夜报～”，内容聚合发送到现有分析师汇总群；群 ID 留空时由用户 OAuth 精确按群名解析。配置路由初始化改为幂等补齐新来源，不覆盖前端已编辑的旧路由。
- 盘后复盘新增 `short-term-review-v1` 七步证据投影：市场情绪（涨停/跌停及昨日涨停溢价）、连板梯队、板块结构、成交额前 20 与龙虎榜、亏钱效应、风向标和次日预案。它只读取当日已落库且满足 `available_at <= observed_at` 的事件/日线/板块证据；板块主线必须有精确成员与报价覆盖，缺失时明确标记，不按中文名称猜归属。
- 收盘复盘前端新增“短线交易七步复盘”卡片，展示证据、覆盖度、风向标、参与条件和失效条件。输出强制 `decision_eligible=false`，不会自动加入观察池、改变阈值或产生订单；这套框架把“情绪—梯队—主线—资金—亏钱效应—风向标—次日计划”固定成可回放清单。
- 盘后一键刷新阶段回执现在支持跨进程恢复：每个 `post-close-refresh:{stage}:{trade_date}` 先读取唯一 `automation_runs` 回执，已 `completed` 的阶段直接返回 `resumed_from_receipt=true`，不重复请求 provider 或写入；`partial`、`blocked`、`failed` 才会重开同一行重试。租约、超时与失败脱敏边界不变，并有真实 PostgreSQL JSONB/时间戳契约测试覆盖。
- AKShare 探针编排已从 `main.py` 移至 `akshare_probe_service.py`：每个 capability 仍保持独立熔断、45 秒有界执行、数据库健康回执、失败脱敏与 `decision_eligible=false`；主文件仅注入已存在的 provider 函数和执行器，不改变接口参数或采集范围。

## 继续迭代顺序

1. 将 `main.py` 中剩余的组合逻辑按“同步任务、市场快照、报告生成”继续抽出；每次只迁移一个调用链，保留兼容导入。
2. 继续扩展 PostgreSQL 契约测试，重点覆盖 Decimal/JSONB、时区边界、盘后阶段回执和清理策略。
3. 将 `automation_runs` 的阶段回执接入板块刷新、盘后候选和周度因子评估，并统一前端任务状态卡片。
4. 将同样的 completed-receipt 恢复语义逐步接入板块刷新、盘后候选和周度因子评估；当前盘后一键刷新已经具备该语义，网络状态仍只是被动观测层。
5. 完成连接池/线程池指标和任务锁治理后，再评估是否需要 Temporal/Celery；当前不引入第二套调度真源。

所有策略与分析师回归继续保持 `live_effect=none`，直到既定样本门禁和样本外验证通过。

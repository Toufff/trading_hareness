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

## 继续迭代顺序

1. 将 `main.py` 中剩余的组合逻辑按“同步任务、市场快照、报告生成”继续抽出；每次只迁移一个调用链，保留兼容导入。
2. 继续扩展 PostgreSQL 契约测试，重点覆盖 Decimal/JSONB、时区边界、盘后阶段回执和清理策略。
3. 将 `automation_runs` 的阶段回执接入板块刷新、盘后候选和周度因子评估，并统一前端任务状态卡片。
4. 为网络恢复场景补充跨进程/重启后的 durable retry receipt（当前进程内网络状态是观测层，持久化任务账本和同步游标是恢复真源）。
5. 完成连接池/线程池指标和任务锁治理后，再评估是否需要 Temporal/Celery；当前不引入第二套调度真源。

所有策略与分析师回归继续保持 `live_effect=none`，直到既定样本门禁和样本外验证通过。

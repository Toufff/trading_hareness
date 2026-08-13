# P0 数据正确性与备份状态

更新时间：2026-08-14（上海交易日口径）

本文件记录已实际落地并验证的 P0 项，避免把设计计划写成已完成事实。

## 已完成

- `DailyBar` 的 `is_st`、`is_suspended` 改为三态输入：普通日线源没有该字段时不会覆盖已有控制面状态。
- `stock_basic` 名称仅以开头的 `ST` / `*ST` 判定 ST，并会更新 `quant.instruments.is_st`。已从 2026-08-11 的主源全市场快照回填 207 只。
- `adj_factor` 归一化时同时回填 `canonical_bars_daily.adj_factor`；日线后续刷新会保留已有因子。因子实验室保留原始 OHLC 作执行/涨跌停判断，但对动量、均线、波动率、IC 和持有期收益显式使用 `price * adj_factor`。
- 腾讯财经公开日线当前明确为 `qfq` 前复权口径。`tencent_free` 已被硬性限制为 raw 研究证据：`persist_free_daily` 不会把它写入 `market_bars_daily` 或 `canonical_bars_daily`，`upsert_bar` 也会拒绝该 source。版本化迁移 `20260811_0003` 已删除旧的 72 条派生行，保留 693 条 raw 证据用于溯源；健康接口显示 `canonical_promotion: false`。
- `suspend_d` 归一化时会将 `[suspend_date, resume_date)` 的已有 canonical 日线标为停牌；普通日线不会清掉该标记。
- `stk_limit` 与 `adj_factor` 的已有控制表记录已回填：当前 canonical 日线中各有 187 条精确值。控制源缺失时，统一回退为主板 10%、创业/科创 20%、北交 30%、ST 5%；精确 `stk_limit` 仍优先。
- 所有原 `date.today()` 默认日期已替换为 `cn_today()`，固定使用 `Asia/Shanghai`。
- 交易日历请求允许且只允许 `trade_cal` 使用年度控制面窗口；`sse_calendar_open` 在本地无记录时默认闭市。已回填 2026 年 248 个 SSE 日历日（242 个开市日）。
- 供应商错误正文在写入/返回前会脱敏 `X-API-Key`、`Authorization`、`token` 等字段，保留无凭据的诊断信息。
- 所有 `POST`/`PUT`/`PATCH`/`DELETE` 量化服务路由由统一中间件保护，要求独立的 `X-Quant-Write-Key`；飞书适配器会转发该 header，5 个活跃 n8n 工作流的 20 个量化写节点则从 `$env.QUANT_WRITE_API_KEY` 读取。四个带已发布版本的定时工作流已额外生成并切换到含 header 的 activeVersion，避免只更新草稿而调度仍执行旧版本。密钥不落入工作流 JSON，也不复用飞书告警 token。
- 已生成并校验当前 PostgreSQL 归档和 n8n 工作流备份；每日 LaunchAgent 运行同一脚本，保留最近 14 天的该脚本备份。它不导出明文凭据。当前调度语义是宿主机洛杉矶时间每天 04:00（上海盘后），使用 `StartCalendarInterval`，不再在登录/重新加载时额外运行。备份先写入受控 `.partial` 临时目录，只有归档清单和工作流导出成功后才原子改名为 `*-daily`；Docker CLI 不可用时不会创建空目录。

## 当前验证证据

- 写入 ST、停牌、复权因子和涨跌停价后，再以普通日线刷新，四个控制字段均被保留。
- 盘中确认前的 `live_policy_gate` 现同时检查板块快照状态、日线复权因子质量和已有纸面组合的暴露/日亏/回撤门禁；观察与风险告警证据仍会落库，不会被静默丢弃。
- 当前全量 316 项测试通过，覆盖上海日期、四种涨跌停规则、复权除权日收益无伪跳空、涨停形态按 20%/30% 缩放、年度日历请求、网关错误脱敏、腾讯前复权隔离，以及真实 PostgreSQL 的 `upsert_bar` 三态 SQL 保护、盘中 episode 链接修复、实时风险门禁、上一报价新鲜度边界、市场状态模块拆分、数值解析模块边界、板块集中度门禁、纸面板块成员点时边界、盘中规则/归因/结算拆分、盘后候选筛选/板块证据聚合及兼容等价性和 provider 延迟保留。
- 手工备份与 LaunchAgent 首次备份均通过 `pg_restore -l` 校验；工作流导出 12 份。
- 认证上线后，缺失 key 的写请求返回 HTTP 401；新增 ASGI `TestClient` 回归覆盖实际服务应用：无 key/错 key 均为 401，正确 key 可抵达受保护路由并进入正常的 422 参数校验。服务重建后全量 316 项测试全部通过；数据库池当前最大上限 12。

## 尚未完成，不能据此过度解释

- 历史控制面只有 187 个日期-证券点，远不足以让既有因子 IC/回测变为可信结果；P2 必须回填日线、`adj_factor`、`stk_limit`、`suspend_d` 的点时历史。
- 本地当前没有 `suspend_d` 记录，因此停牌同步逻辑已修复但尚无真实样本回填。
- 复权因子可能被供应商事后修订；当前仅保证原始价格不被混写，历史 PIT 因子快照仍需 P2 完成。
- 历史分钟回放尚未启用；数据库连接池和有界线程执行器已启用并通过开盘预检。

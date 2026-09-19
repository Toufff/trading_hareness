# 盘后研究闭环与全 A 闸门修正（2026-09-18）

状态：设计已定，实施中。本文件是本次修改的合同；实现与本文冲突时先改本文。

## 背景（2026-09-18 实测）

1. `quant.universe_membership_history` 的 all_a 宇宙在 09-18 10:31 由 `stock-basic-all-a:tushare_super_get` 写入 312 只北交所 920 开头代码（另 12 只新股由 longhu 16:41 加入），预期数从 5258 跳到 5563；开盘啦日线不含北交所，`daily_control_plane` 的全 A 闸门因此从 98.1% 掉到 92.1%、blocked。日线本身没有缺口（日常流动：前三日分别掉 104/83/113 只、新增 90/107/82 只）。
2. 闸门报错只给 “5122/5563”，不说分母为何变化；`stock-platform-context.py` 也不输出宇宙数日间变化，导致误判为“缺 113 只日线”。
3. 流水线在 `review_coverage.completed=0` 时仍把整轮记为 `completed`，`research_status=screening_only` 被当作合法终态；公司研究、推荐池决策、同花顺同步三步没有任何自动触发。每轮结束拉起的 governance 侧车是策略治理，不是公司研究，日志状态名 `governance_dispatched` 易被误读。

## 修改 A：全 A 闸门按交易所分开计算并输出漂移诊断

文件：`quant-service/app/daily_control_plane.py`、`scripts/equity-readiness.py`、`scripts/stock-platform-context.py`（或其读取的 context 模块）、对应测试。

- `EQUITY_DAILY_CONTROL_STATUS_SQL` 改为按 `exchange`（symbol 后缀 SH/SZ/BJ，无后缀归入 `UNKNOWN`）分组统计 expected 与 daily/adjustment/limit 行数，**每个交易所一行、不返回总行**；总量由 `status_payload` 在 Python 侧求和，全 A 合计只以 `all_a: {expected_daily_rows, daily_rows}` 暴露（不含 adjustment/limit）。因此每个调用方都必须 `fetchall()`，`status_payload` 收到单行会直接 `TypeError`，不会把某一个交易所当成全市场。
- `status_payload` 新增：`by_exchange: {SH:{expected,daily,ratio}, SZ:{...}, BJ:{...}}`；`gating_exchanges: ["SH","SZ"]`（北交所在有日线源前不参与门槛）；`coverage_ratio`、`minimum_required_rows`、`state` 只按参与门槛的交易所计算；`ungated_exchanges` 列出未参与门槛的交易所及其覆盖，供报告展示。
- 门槛成员资格由**唯一常量 `UNGATED_EXCHANGES = ('BJ',)` 以排除法**决定：`status_payload` 与 `daily_row_count` 共用同一定义，未知后缀因此在两侧都留在门槛内，同步与门槛不可能对同一交易日给出不同口径（`GATED_EXCHANGES` 仅用于 `gating_exchanges` 的展示与文案）。
- 新增 `expected_previous_trading_day` 与 `expected_delta`（与上一交易日的 all_a 预期数差值，按 `quant.universe_membership_history` 用 `effective_from/effective_to` 回算）；`|expected_delta| > 2% × expected` 时 `reason` 里必须写明 delta 和来源分组，来源分组取 `effective_from = 当日` 且当日仍有效的行按 `source` 计数。
- `expected_delta` 是两个时点总量之差（净额），来源分组是当日起算的 membership 行数（含对已有成员的重新写入），**两者本就不相等**：09-18 实测 delta=+305、当日新增行=324。文案必须把两者写成两个独立量（`{delta:+d}（当日新增 N，来源分组：…）`），不得拼成一个看似可以对平的算式。
- `reason` 文案在 blocked 时必须同时给出分子、分母、按交易所的覆盖和 delta，例如 “SH+SZ 5093/5221=97.5% ready；BJ 29/342 未参与门槛；all_a 预期较上一交易日 +305（当日新增 324，来源分组：stock-basic-all-a:tushare_super_get 312, longhuvip_composite 12）”。
- `scripts/stock-platform-context.py` 输出的 `equity_readiness` 原样带上这些新字段。
- 测试：`tests/test_daily_control_plane.py`（若已存在则扩展）：BJ 缺席不阻断、SH/SZ 低于 95% 阻断、delta 文案、来源分组；SQL 只在 fake-connection 上验证参数与分组键，不连库。
- 不改宇宙同步本身（all_a 包含北交所是既定语义，`universe_history.py` 的正则也含 BJ），不删除任何 membership 行。

## 修改 B：流水线记录研究待办状态

文件：`scripts/windows/run-post-close-pipeline.ps1`、PowerShell 契约测试（`scripts/windows/tests/` 下新增 `test-post-close-pipeline-contract.ps1`，用与其它 `test-*.ps1` 相同的方式做静态/函数级断言）。

- `research_status` 取值改为 `complete | research_due`（不再叫 screening_only）；为 `research_due` 时记录 `research_deadline`（当日 21:30 Asia/Shanghai）、`research_missing_symbols`（来自 review_coverage.missing_symbols）、`research_owner: "stock-scan post-close session"`。
- 只有 `review_coverage` **完整可用且自证完成**才算 `complete`：`missing_symbols` 为空列表、`planned` 与 `completed` 都存在且 `completed >= planned`。`review_coverage` 缺失、`missing_symbols` 缺字段或为 JSON null、`planned`/`completed` 任一缺失、或 `completed < planned`（如 `{planned: 9, completed: 0}`）一律 fail closed 记 `research_due`——这正是本次要消除的“零研究却记 completed”。
- 记录顶层 `status` 规则：扫描与发布成功但 `research_status=research_due` 时 `status='completed_research_due'`（不再是 `completed`）；其余不变。`strategy_status` 仍为 `completed`，与研究状态解耦。
- governance 侧车的记录状态名改为 `strategy_governance_dispatched` / `strategy_governance_dispatch_failed` / `strategy_governance_waiting`，`reason` 文案加 “策略治理侧车，不是公司研究”。
- 任何读取 `pipeline_status`/`status` 的下游若对 `completed` 做了等值判断，改为调用共享的 `Test-PostClosePipelineCompleted` / `Get-PostClosePipelineCompletedStatus`（`post-close-contract.psm1`），同时接受 `completed_research_due`；用 grep 找全，不能漏。实测只有 `run-post-close-pipeline.ps1` 与 `test-post-close-publication-live.ps1` 两处，均已迁移；`get-stock-release-status.ps1` 只读 release 布局与 manifest，从不读流水线状态，不在此列。

## 修改 C：研究触发（会话级，不在仓库内）

由 Claude 会话的 cron 承担：工作日 17:20 触发 `/stock-scan 盘后研究`，去重标记 `tail-done` 同款，读取 `post-close-pipeline.jsonl` 最新一轮；`research_status=research_due` 时按 `references/research-closure.md` 完成 review_plan 全部对象、`--review-file` 落库、重建报告，再按用户既有授权走 `$stock-pool` 决策与 `$ths-watchlist` 同步。仓库内只需保证修改 B 的字段可读。

## 修改 D：16:00 起跑与迟到数据集闸门（2026-09-19）

文件：`scripts/windows/install-post-close-pipeline-task.ps1`、`scripts/windows/run-post-close-pipeline.ps1`、
`scripts/windows/post-close-contract.psm1`、`scripts/equity-readiness.py`、
`quant-service/app/settled_limit_pool_repository.py`，及两侧测试。

- 首跑时间 16:40 → **16:00**，重试窗口 6h → 7h（末次 23:00，仍在 `-UntilHHmm 2330` 内）；
  `run-post-close-pipeline.ps1` 的 `-AfterHHmm` 默认同步改为 `1600`。
- `-AfterHHmm` **只是时钟下限，不是就绪判断**。旧注释写的“16:30 之前日线有了但涨停池还没有”，
  在当前开盘啦（longhu）链路上已经不成立：收盘涨停池由
  `settled_limit_pool_repository.persist_settled_limit_pool` 从本库已有的
  `canonical_bars_daily` + `daily_trade_limits` **派生**，不向任何厂商再取一次数，
  因此它相对厂商不会迟到，只会相对**我们自己**迟到——当 `limit_ladder` 阶段跑在结算日线到齐之前、
  或该阶段返回 blocked 时，池子会比日线该有的短。
- 实测（10 个交易日，只读）：授权收盘截面最早可用时间在 15:58—16:41 之间浮动，
  16:00 不保证到齐；但 `Test-EquityDateReady` 对半截截面本来就 fail closed
  （2026-09-18 即如此：16:43—18:42 连续记 `partial`，20:29 才 `completed`）。
  派生涨停池在 12 个 longhu 结算日上 expected 与 stored **全部相等**，未观察到偏差。
- 缺口在**同日跳过条件**：它只校验 equity readiness + lane 版本三连 + 报告文件回读，
  从不看 `limit_ladder`。而顶层 `status` 的 `Degraded` 只取 `market_refresh_error` /
  `ingestion_error`（都是异常/截面闸门），刷新返回体里 `stages.limit_ladder.status='blocked'`
  只写进 `refresh_stage_diagnostics`、不降级。两者相加：一轮池子残缺的运行会被记成
  `completed`，随后每半小时都 `skipped`，把残缺的一天封存到收盘窗口结束。
- 闸门做法：`scripts/equity-readiness.py` 在 `--date` 模式下多返回一段
  `late_datasets.limit_pool = {expected_symbols, stored_symbols}`。
  `expected_symbols` 用**写入池子的同一条谓词**（`CLOSED_AT_LIMIT_PREDICATE`，
  `close >= limit_up - 0.005`）从**已有日线**里数，绝不看时钟；
  `daily_trade_limits` 一个 symbol 有多家 provider 行，join 会扇出，所以必须
  `count(DISTINCT bar.symbol)`（2026-09-18 用 join 行数会把完整的 80 只误读成 155）。
  `stored_symbols` 按 `event_identity_key`（含交易日）统计，不按 `occurred_at` 的日期——
  派生行的时间戳取自日线 `available_at`，补历史时会跨到次日。
- 判定放在纯 PowerShell 的 `Get-PostCloseLateDatasetState` / `Test-PostCloseLateDatasetsReady`
  （`post-close-contract.psm1`），一律 fail closed：探针缺失、探针日期对不上、计数字段缺失都算未就绪；
  `expected > 0 且 stored < expected` 算未就绪；`stored > expected` **不算缺陷**
  （upsert 不删行，限价修正会留下旧行）；`expected = 0` 没有可违反的期望，放行。
- 接线两处：同日跳过条件加 `$lateSkip['ready']`；记录路径把
  `-not $lateDatasets['ready']` 并入 `Degraded`，于是该轮记 `partial`（非终态成功）、
  `exit 1`，下一次半小时重试因跳过条件不成立而重新跑刷新与 `limit_ladder`。
  记录里带 `late_datasets`（含 expected/stored 与 reason）便于事后判读。
- 刷新后必须**重新探一次** readiness：`$after` 是在市场刷新之前读的，看不到本轮刚派生出的池子。

## 修改 E：推荐模型使用当日收盘（2026-09-19）

**2026-09-19 用户决定：推荐模型使用当日收盘（available_at 约束保留，次日开盘入场），推翻 9/3 审计的严格小于。**

文件：`quant-service/app/feature_snapshot_repository.py`、`quant-service/app/daily_pipeline.py`，
测试 `tests/test_feature_snapshot_repository_pit.py`、`tests/test_daily_pipeline_stage_order.py`、
`tests/test_platform_boundaries.py`、`tests/test_ingestion_and_provider_runtime.py`。

- 背景：多源推荐（`multi-source-feature-v3` / `multi-source-direction-v1`，`/api/v1/pipeline/daily`
  的最后一个决策阶段）一直滞后一个交易日——2026-09-18 的运行 `explanation.market_data_date`
  是 2026-09-17。原因是 2e996ca（9/3 审计）把特征快照的日线、基础指标、ST 生命周期查询从 `<=`
  改成了 `trading_date < as_of_date`。
- 为什么不是前视：盘后流水线传入的 `as_of` 就是已结算的交易日 D；推荐结果归因
  （`outcome_recomputation.py`）和策略候选账本结算都以**严格晚于 run_date 的第一根日线**
  （D+1 开盘）入场，所以特征日期 D 永远早于入场日期 D+1。测试
  `SameDayFeatureNextSessionEntryTests` 把这两半钉在一起。
- 新谓词（日线与 `daily_fundamentals`）：
  `trading_date <= as_of AND available_at <= observed_at AND (trading_date < as_of OR available_at >= as_of 15:05 上海)`。
  `available_at <= observed_at` 原样保留（防历史回放读到之后补录/更正的行）；
  追加的结算条件复用 `public_market_repository.SESSION_SETTLED_TIME`（15:05）：
  当日行只有在收盘结算后才可用。ST 生命周期证据是状态而非当日结果，只改为
  `status_date <= as_of`，`available_at` 约束不变。
- 盘中调用方核查：没有定时任务在盘中调用 `materialize_feature_snapshot`，唯一入口是
  `build_feature_snapshot`（`/api/v1/features/build`、`/api/v1/recommendations/generate`、日流水线）。
  但前端研究台“构建特征/生成推荐”按钮不带 `as_of_date`，会以 `cn_today()` 在盘中任意时刻触发；
  而 `canonical_bars_daily` 只有公开源路径（`persist_free_daily`）拒收未结算当日行，
  Tushare 兼容归一化、`persist_daily_bar_batch` 等路径没有该守卫，且默认 `observed_at`
  是当日 23:59:59。单靠 `available_at <= observed_at` 拦不住一根 10:30 写入的盘中日线，
  所以加了上面的 15:05 结算条件：15:05 前的盘中调用行为与旧的严格小于完全一致。
- 生产只读核对（2026-09-19，all_a 5571 个启用成员）：旧谓词下 2026-09-18 快照最新日线全部是
  09-17（读到 9/18 行 0 条）；新谓词下 5122 只的最新日线为 09-18，9/18 行 5122 条，
  `available_at` 全部为 2026-09-18 16:41:35（均晚于 15:05，结算条件不剔除任何一条）；
  9/18 `daily_fundamentals` 5122 只全部可用；9/18 ST 生命周期证据 0 条（回退当前状态，与原逻辑一致）。
  8/1—9/18 的 canonical 日线中有 244 行 `available_at` 早于其交易日 15:05
  （243 行 `legacy:yahoo_chart` 每日约 9 只、时间戳 14:35，1 行 `legacy:longhuvip:GetStockPanKou`），
  这些行在以其自身交易日为 as_of 的回放里会退回前一交易日（fail closed），作为前一日特征不受影响。
- 同时修正日流水线顺序：`materialize_candidate_ledger` 会把当日 `quant.recommendations`
  写入 `daily_recommendation` 账本行，原先排在 `generate_recommendations` 之前，
  当日推荐只有重跑才进账本。现改为 `generate_recommendations → materialize_candidate_ledger →
  materialize_watchlist_proposals`。中间阶段不依赖账本：`recompute_outcomes` 只结算入场日
  （严格晚于候选日期的第一根日线）已存在的账本行，当日候选当日不可能有入场日。
  副作用：推荐阶段若抛错，账本/观察建议也不再执行（原先推荐抛错整轮同样失败，重跑补齐）。
- feature_version 随此改动升为 `multi-source-feature-v4`：v4 快照读当日收盘，v3 快照只读到前一交易日，
  两者在 as_of 语义上相差一个交易日；跨版本比较 run 级指标时按 `market_data_date` 对齐。

## 验收

1. `pytest tests/test_daily_control_plane.py tests/test_migration_contracts.py tests/test_repository_workflow_policy.py -q` 通过；`ruff check app tests` 通过；`git diff --check` 通过。
2. PowerShell：`pwsh -File scripts/windows/tests/test-post-close-pipeline-contract.ps1` 通过，且现有 `test-*.ps1` 不受影响。
3. 用生产库只读跑 `scripts/equity-readiness.py --date 2026-09-18`：期望 `state=ready`，`by_exchange.BJ` 显示 0 覆盖且未参与门槛，`expected_delta=+305` 并带来源分组。
4. 发布后 16:00—23:00 重试窗口（2026-09-19 起；原 16:40—22:40）外用 `-Force` 重跑一次流水线，日志出现 `research_status=research_due` 与 `research_deadline`，随后由会话完成研究闭环，再次读回应为 `complete`。
5. 修改 D：`scripts/equity-readiness.py --date <交易日>` 只读跑生产库，`late_datasets.limit_pool` 的
   `expected_symbols` 与 `stored_symbols` 相等（2026-09-18 为 80/80，2026-09-15 为 31/31）；
   `pytest tests/test_post_close_refresh.py -q` 与
   `pwsh -File scripts/windows/tests/test-post-close-pipeline-contract.ps1` 通过。
6. 修改 E：`pytest tests -q` 全绿；生产只读查询 2026-09-18 新谓词读到 9/18 日线 5122 行（旧谓词 0 行）；
   发布后首个盘后流水线的 `quant.recommendations.explanation->>'market_data_date'` 等于 run 的 `as_of_date`，
   且 `strategy_daily_candidates` 当日即出现 `daily_recommendation` 行（无需重跑）。

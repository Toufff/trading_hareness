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

## 验收

1. `pytest tests/test_daily_control_plane.py tests/test_migration_contracts.py tests/test_repository_workflow_policy.py -q` 通过；`ruff check app tests` 通过；`git diff --check` 通过。
2. PowerShell：`pwsh -File scripts/windows/tests/test-post-close-pipeline-contract.ps1` 通过，且现有 `test-*.ps1` 不受影响。
3. 用生产库只读跑 `scripts/equity-readiness.py --date 2026-09-18`：期望 `state=ready`，`by_exchange.BJ` 显示 0 覆盖且未参与门槛，`expected_delta=+305` 并带来源分组。
4. 发布后 16:40—22:40 重试窗口外用 `-Force` 重跑一次流水线，日志出现 `research_status=research_due` 与 `research_deadline`，随后由会话完成研究闭环，再次读回应为 `complete`。

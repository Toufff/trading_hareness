# stock-brain migration into trading_hareness

## 2026-09-02 方法修订

审计整改（WP4 数据正确性/点时边界、WP5 统计有效性与结算）修订了本文档"Implemented runtime and audit
endpoints"一节所依赖的结果结算与事件可得性口径：涨停池/龙虎榜等事件的可得时刻改为盘后 15:30
（`availability_basis="post_close_publication"`）、未收盘的日线不再进入 canonical 表、退市/停牌样本改为
按交易日历正确纳入结算而不是静默排除或借用错误价格、多重比较统计接入了 Deflated Sharpe Ratio 与
Benjamini-Hochberg 修正、`methodology_version` 现在有实际语义并归档旧计算结果。完整清单见
[`STRATEGY_LOOP_V1.md`](STRATEGY_LOOP_V1.md) 的同名章节。**修订前的 replay/命中率数字（包括本文件"Current
migration acceptance snapshot"一节记录的 2026-09-01 结果）不可与修订后的数字直接比较**，需要对受影响区间
重新调用相应的结算函数才能得到可比数字。已退休的 `decision_research_closure`/G0--G7
dossier 只保留为历史审计资料，不再进入新平台扫描、公司研究、推荐池、报告或网页。

## Decision

`trading_hareness` becomes the long-term market-data, research-runtime and
decision-product host.  `stock-brain` is a migration source, not a library to
embed wholesale.

The old action-card orchestration, dependency cascade and scheduler state are
not migrated.  Their production history did not meet the live delivery
acceptance threshold.  Only durable facts, evidence, user constraints and
settled outcomes are eligible for import.

## Ownership after migration

```text
market providers / announcements / analyst media
                    |
                    v
trading_hareness evidence and strategy platform
 raw -> canonical -> features -> signals -> outcomes
                    |
                    +-----------------------+
                    |                       |
                    v                       v
             market decision         candidate research
                    |                       |
                    +-----------+-----------+
                                |
Windows CITIC read-only bridge  |
      BrokerPortfolioSnapshot   |
                    |           |
                    +-----------+
                                v
                    personal decision brief
           market / holdings / new buys (independent)
```

The Windows bridge may read a user-selected, already logged-in desktop broker
client but must never place, amend or cancel an order. The default path is a
user-triggered desktop export, with Luna-owned UI reading as fallback. MuMu is
not part of the default holdings path; explicit MuMu diagnostics remain
read-only. The research service never controls a desktop client or emulator
directly. It only accepts immutable, timestamped broker snapshots through a
versioned contract. THS desktop reading is not yet real-world accepted.

## Migration classes

### Import as durable facts

- settled daily and minute bars with provider and availability timestamps;
- sector membership, sector flow and stock order-size flow observations;
- broker position snapshots and journalled user trades;
- source documents, primary-evidence references and verified company facts;
- prediction, candidate and strategy outcomes with their original model
  versions and point-in-time boundaries.

The active bridge imports one user-triggered, exact broker snapshot from the
current desktop export/UI evidence. It requires one observation timestamp across
the account and every position, a broker read-only source marker, a real broker
screenshot path and market-value reconciliation within 0.1%.  It deliberately
drops every legacy `plan` and `trigger` field.  Dry-run is the default; API
publication requires `--apply` and `QUANT_WRITE_API_KEY`, followed by exact
readback of the source snapshot key.

### Re-implement against the new contracts

- actual-portfolio read model;
- company research terminal verdict;
- holding and new-buy trade plans;
- personal decision brief and its dashboard;
- scheduled decision publication and live acceptance receipts.

### Archive only

- old action-card and decision-session rows;
- transient task queues, caches and generated Markdown reports;
- incomplete research rows whose evidence cannot be reconstructed;
- paper positions presented as if they were actual broker holdings.

## Contracts

### BrokerPortfolioSnapshot

An immutable observation from a read-only broker bridge.  It contains the
account key, source snapshot key, timezone-aware observation time, verification
state, account totals, positions and source metadata.  Reusing a source key
with different content is a hard conflict.

### PersonalTradePlan

A terminal human-facing plan.  A new-buy plan is not admissible without a
bounded entry zone, invalidation trigger, stop, maximum position and evidence
references.  Research candidates and unfinished work never enter this table.

### PersonalDecisionBrief

The three sections are independent:

1. market and sector state;
2. actions for the latest verified actual holdings;
3. fully researched new-buy plans.

A stale or missing broker snapshot blocks holding actions but cannot erase the
market section or eligible new-buy plans.  Diagnostics are retained separately
from human-facing action text.

### Retired DecisionResearchDossier / G0--G7

The 2026-09-01 migration temporarily introduced a synthetic G0--G7 dossier
layer.  Live evidence showed that it could mark a batch completed while doing
no candidate company review, so the runtime service, routes, report projection
and public UI were retired on 2026-09-15.  Its tables and migration remain only
to preserve historical audit rows; no active module may import or execute it.

The current decision chain is explicit and independently testable:

1. `short_term_lanes` persists the full nine-lane screen and its review plan;
2. `short_term_company_review` records actual primary-source company review;
3. `recommendation_pool_decisions` compares the full candidate population,
   prior internal pool and user tracking, then publishes one versioned result;
4. broker holdings remain a separate, user-triggered read-only fact stream.

A successful scan/report publication is therefore not called completed company
research or a ready recommendation.  The public market page reads the formal
recommendation pool and never projects historical G0--G7 rows.

## Deployment boundary

- Windows workstation: the authoritative PostgreSQL cluster, raw evidence,
  canonical market data, research workers and broker read-only bridge.  The
  durable root is `G:\StockPlatform` on the local 12 TB data disk; repository
  checkouts and generated secrets are not stored in the database directory.
- lightServer: optional static frontend/reverse proxy only.  It must not become
  the authoritative database or a second writer merely to publish the UI.
- Transport: authenticated, versioned HTTP.  The emulator process never opens
  PostgreSQL directly, and no component shares the legacy SQLite file after
  cutover.
- Backups: local PostgreSQL dumps and immutable evidence archives are written
  under the G-drive platform root first, then may be copied to independent
  remote/object storage.  A frontend deployment is never treated as a backup.

## Cutover acceptance

Cutover requires at least five consecutive trading days in shadow mode with:

- one immutable run receipt per expected phase;
- current market and sector evidence even when the broker bridge is unavailable;
- exact broker snapshot readback for every holding action;
- no unfinished-research prose in the user-facing brief;
- every visible buy plan carrying entry, invalidation, stop, position cap,
  target and validity window;
- dashboard and published brief resolving to the same content hash;
- explicit reason codes for every blocked section.

Source-only unit tests are necessary but never sufficient for cutover.

## Implemented runtime and audit endpoints

The Windows runtime uses PostgreSQL 16 on `127.0.0.1:55432`; its data directory
is `G:\StockPlatform\data\postgresql16`.  Runtime configuration is external to
Git under `G:\StockPlatform\config`, and large imports and raw research files
remain under `G:\StockPlatform\data`.  The repository must contain only code,
migrations, bounded fixtures and documentation.

The post-close orchestration now publishes settled market evidence and the
nine-lane scan.  Company review coverage and formal recommendation status are
separate fields in the same dated read model.  The active personal surface is:

- `GET /api/v1/personal/portfolio-snapshots/latest?account_key=...`;
- `GET /api/v1/personal/decision-briefs/latest?account_key=...`;
- `GET /api/v1/strategy/post-close/latest?as_of_date=...`.

Market/recommendation and holdings are intentionally independent reads.  A
broker failure only blocks the holdings section and cannot filter the full
market candidate/recommendation surface.  The frontend displays the stock name
first and its exchange code in parentheses on every first mention.

### Current migration acceptance snapshot

The following paragraph is a historical migration receipt, not the active
acceptance contract.  The 2026-09-01 settled-close replay persisted an
explicitly `degraded` market review (the current
multi-index/breadth evidence was incomplete), two exact holding dossiers,
twelve bounded candidate dossiers, one qualified conditional-buy plan and two
holding plans.  The current audit read returned 14 dossiers from the latest
model/current candidate batch only.  Those dossiers are retired historical
evidence and cannot satisfy current research coverage.  This is an integration
receipt, not an investment recommendation and not the
five-day shadow cutover required above.  A degraded market section remains
visible but can never make the overall decision brief claim `ready`.

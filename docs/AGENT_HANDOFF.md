# Agent Handoff Runbook

This is the authoritative entry point for an agent taking over the running
Windows research platform. Read it together with the repository `AGENTS.md`;
do not infer production state from the development checkout.

Remote peer pool recovery (2026-09-14): see [PEER_POOL_RECOVERY_20260914.md](PEER_POOL_RECOVERY_20260914.md).
Follow-up network fix deployed at 18:42 CST: [PEER_PRIVATE_TUNNEL_20260914.md](PEER_PRIVATE_TUNNEL_20260914.md).
The 301-symbol compatibility regression and public `/api/config` gateway fix are documented in [PEER_LONGHU_BATCHING_20260915.md](PEER_LONGHU_BATCHING_20260915.md).
The same-host db-tunnel now uses the verified private route plus a real PG query healthcheck;
preserve this configuration instead of restoring a public-IP hairpin.
The two lightServer peer containers have a minimal, verified empty-pool recovery hotfix;
do not overwrite them by rebuilding the server's old checkout. This is not a claim
that the collaborator's separate edge/Mac services have been repaired.

Short-term multi-lane scanning: see [SHORT_TERM_STRATEGY_LANES.md](SHORT_TERM_STRATEGY_LANES.md).
Publication recovery and real scheduled-entry acceptance: [PUBLICATION_RECOVERY_20260915.md](PUBLICATION_RECOVERY_20260915.md). Never use an acceptance helper that re-exports files to prove the scheduled pipeline completed; run the production entry via the documented disposable task.
User-triggered intraday scanning: use [INTRADAY_STRATEGY_SCAN.md](INTRADAY_STRATEGY_SCAN.md); no ad-hoc lunch scoring. Preserve original recommendations and distinguish structure confirmation from prospective price-trigger evidence.
Strategy governance and current price-volume changes: read [STRATEGY_GOVERNANCE_IMPLEMENTATION.md](STRATEGY_GOVERNANCE_IMPLEMENTATION.md) for exact acceptance scope and remaining gaps; [STRATEGY_GOVERNANCE.md](STRATEGY_GOVERNANCE.md) for contracts. Never describe configuration engineering experiments as proven profitable strategies or arbitrary-code autonomous implementation. Local independent role identities/executable are provisioned in `G:/StockPlatform/config/governance-actors.json`.

Effectiveness-loop implementation (2026-09-13): [STRATEGY_EFFECTIVENESS_LOOP.md](STRATEGY_EFFECTIVENESS_LOOP.md) supersedes the earlier blanket statement that there is no effectiveness executor. `app/effectiveness` provides date-paired diagnostics and a finite registered future-shadow executor, NOT arbitrary code execution and NOT proven profit. Each close scan attaches the same result to ten reports and the UI. Human recommendation registration is explicit via `scripts/record-manual-recommendation.py`, never retrospective attribution. Acceptance receipts: `G:/StockPlatform/data/research/effectiveness-acceptance`; distinguish rollback-only fixtures, actual ledger counts, deployed scan/API, and browser checks.
Opt-in, strategy-scoped factor profiles: see [OPTIONAL_RANKING_FACTORS.md](OPTIONAL_RANKING_FACTORS.md). Small-cap preference is OFF by default; a one-off user request must not silently change all scheduled strategies.
Interactive stock-workbench presentation control: see [STOCK_STRATEGY_WORKBENCH.md](STOCK_STRATEGY_WORKBENCH.md#agent-动态演示控制).
The close job must verify persisted strategy lists, not stop at market ingestion.
Each scan now publishes nine independent strategy reports plus one overview in
`G:\StockPlatform\reports\short-term`, also persisted as `strategy_lanes.report_bundle`.
The scheduled runner verifies all report bodies against owner and dashboard API
readback; publication verification is not a claim of complete company research.

## Deployment identity

```text
Development checkout
  F:\AIWorkflow\trading_hareness

Immutable production releases
  G:\StockPlatform\releases\<release-id>\app

Active production junction
  G:\StockPlatform\current

Release state
  G:\StockPlatform\release-state.json

Authoritative PostgreSQL data, hot tier (NVMe)
  F:\StockPlatformDB\postgresql16        <- PGDATA_DIR in runtime.env; never hardcode it

Same database, cold tier (tablespace stock_cold, HDD)
  G:\StockPlatform\data\pg-cold

Private runtime configuration
  G:\StockPlatform\config\runtime.env

Runtime logs and lifecycle evidence
  G:\StockPlatform\logs\runtime

Peer credentials, exports and staging
  G:\StockPlatform\peer
```

数据库的物理布局（热层预算、冷层表空间、维护窗、备份链、数据目录迁移与回滚）以
[OWNER_DATABASE_STORAGE.md](OWNER_DATABASE_STORAGE.md) 为准。动数据目录、备份、
定时维护任务或任何分层表之前先读它；上面的路径只是速查，权威值是 `runtime.env` 里的
`PGDATA_DIR`。接手时必须知道的几条：

- **分层作业还没跑过 `install`。** 生产库里没有任何 `*_cold` 表、`*_tier_cutoff_idx`
  索引、`quant.storage_tier_conflicts` 表，也没有 `stock_cold` 表空间；
  `runtime.env` 里也还没有 `PGDATA_DIR` / `PGDATA_BUDGET_BYTES` /
  `PGDATA_COLD_TABLESPACE_DIR` / `STOCK_BACKUP_INCREMENTAL_TABLES` /
  `STOCK_BACKUP_EXCLUDE_TABLE_DATA` / `STORAGE_TIER_HOT_DAYS` 任何一个。
  活机上什么都还没被改动。
  **因此现在跑 `status` / `plan` 会答 `degraded` 退出 2，跑 `apply` 会答 `partial` 退出 1**
  ——这是设计出来的答案，说的就是"`install` 还没跑"，不是回归。
  从前这种状态报 `ok` 退出 0，任务计划程序上一片绿而守卫一夜没守过。
- **两个新任务还没注册**：`trading-hareness-storage-tiers`（每日 06:00，
  `run-storage-tiers.ps1 -Command apply`，`ExecutionTimeLimit` 2h15m）和
  `trading-hareness-postgres-io-window`（每 15 分钟）。两个安装器的
  `-RepositoryRoot` / `-HostRoot` 默认就是 `G:\StockPlatform\current`，
  必须在带这些脚本的 release 发布之后从 `current` 手工运行一次；
  它们会把实际写进任务的 `Execute` 路径打印出来。
- **作业自己会停**：runner 给 `apply` 传 `--deadline 08:00` 和 `--max-seconds 7200`，
  到点写一条 `status='deadline_reached'` 的回执并以 **0** 退出，明晚接着搬。
  2h15m 的 `ExecutionTimeLimit` 只是兜底——被它杀掉的进程不写任何回执。
  起跑时截止时间**已经过去**（`-StartWhenAvailable` 补跑到了窗口之外）是另一回事：
  记 `deadline_missed`、告警、退出 **1**，因为那一夜一张表都没碰。
- **退出码：0 = 正常（含 `deadline_reached`）；1 = 需要人看但分层完好
  （`deadline_missed` / `partial` / `conflicts` / `schema_drift`）；2 = 500 GB 守卫失效
  （`degraded`：用量测不出来、热窗到底、棘轮停在 `needs_repack`，或 `install` 没跑过）。
  不要给它加自动重试，尤其不要把 2 当成"重试一下就好"。
  任务本身也刻意**不注册失败重启**：每跑一次最多砍某张表 7 天热窗口，重启两次就是 21 天，
  而每份回执单看都"合规"。**
- **分层作业会读增量备份链的水位线**（`<STOCK_BACKUP_ROOT>\incremental\<表>\state.json`），
  绝不把任何一行搬到水位线之上。链落后时记 `chain_behind`（退出 0，钳位干活了），
  链读不出来时**那张表一行都不搬**并记 `chain_missing`（退出 1）。
  所以增量导出连着失败不只是备份的问题，修它优先于调分层策略。
- **`stock_peer` 角色超时已于 2026-09-19 在生产集群生效**：
  `statement_timeout = 15min`、`idle_in_transaction_session_timeout = 5min`。
  这是集群级设置，peer 侧的长查询会被打断，需要更久请显式 `SET LOCAL`。
- **第一次 `apply` 会搬 0 行，这是正确结果。** 最老的热行约 156 天，热窗 365 天，
  用量约 33 GB / 500 GB；未来七个月左右都会是 0 行。
- **别手工往 `STOCK_BACKUP_EXCLUDE_TABLE_DATA` 里加冷孪生表。** 每夜 dump 的排除列表
  是在 dump 时按 `STOCK_BACKUP_INCREMENTAL_TABLES` 算出来的：没有分块链、**或链的水位线
  已经落到分层截止线之前**的孪生表都会被拒绝并告警（热窗口取 `STORAGE_TIER_HOT_DAYS`，
  默认 365；调窄 `--hot-days` 时必须同步设它）；
  `quant.storage_tier_conflicts` 永远不能进排除列表（它可能是某行热数据的唯一副本）。
- **数据目录迁移 `-Rollback` 需要 `-AcceptDataLoss`**，而且有两条**绝对拒绝**（没有开关可绕）：
  活集群已有 `stock_cold` 表空间而镜像早于它；或者两边 `pg_tblspc` 指向**同一个**表空间目录
  而活集群的 checkpoint 更新（冷层从来没被复制或版本化过，那种回滚是自相矛盾的集群）。
  每次 `-Rollback` 都会打印并记录 `stock_cold_reverted: false`。
  迁移回执里打印的回滚命令故意不带 `-AcceptDataLoss`，照抄会被拒绝。
  迁移**不停** `trading-hareness-shared-peer-tunnels`（停 PostgreSQL 本身已经切断了 peer 会话），
  行数快照在**停机窗口内**抓，失败时会把留在目标目录的半成品拷贝删掉再退出。

持仓同步契约见 [BROKER_HOLDINGS_SYNC.md](BROKER_HOLDINGS_SYNC.md)：现在只由用户主动触发，文件导出优先，桌面 UI 读取必须直接交给 Luna 子 agent。不得创建每日调度、自动登录或自动唤醒 MuMu；未指定券商时不得默认中信。当前 THS 桌面读取尚未真实验收，旧 `citics-mumu-sync` 仅为退休兼容入口。

The scheduled production services must execute only from
`G:\StockPlatform\current`. Never edit that junction or a release directory.
Make changes in the F-drive development checkout, run tests, and publish a new
release. Production configuration and database files never enter Git or a
release snapshot.

Production keeps three releases: the active release, the immediately previous
known-good release, and one additional recent fallback. This is a bounded
rollback window, not an archive. Git remains the source-code history.

## First five minutes

Run these read-only commands before changing anything:

```powershell
cd F:\AIWorkflow\trading_hareness
git status --short --branch

pwsh .\scripts\windows\get-stock-release-status.ps1
pwsh G:\StockPlatform\current\scripts\windows\get-stock-runtime-status.ps1
pwsh G:\StockPlatform\current\scripts\shared-peer\verify-shared-runtime.ps1

Get-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime','trading-hareness-shared-peer-tunnels' |
  Select-Object TaskName,State
```

Expected production services and ports:

| Service | Local endpoint | Remote relay |
|---|---|---|
| quant owner API | `127.0.0.1:5681` | lightServer `127.0.0.1:15681` |
| dashboard adapter | `127.0.0.1:5680` | lightServer `127.0.0.1:15680` |
| PostgreSQL | `127.0.0.1:55432` | lightServer `127.0.0.1:15432` |
| collaborator API | remote only | lightServer `127.0.0.1:15682` |

The public dashboard gateway intentionally splits dynamic routes.  Browser
compatibility endpoints under `/api/research/`, `/api/config` and `/health` go to the adapter
relay at `15680`; owner strategy endpoints under `/api/v1/strategy/` go to the
owner API relay at `15681`.  Other `/api/` routes remain on the server's native
read service.  After changing the split, deploy
`deploy/stockbrain-local-gateway.nginx.conf` and run
`scripts/verify-public-gateway-split.ps1`; a successful static deployment alone
does not prove the API routing is correct.

Task Scheduler result `267009` (`0x41301`) means a long-running task is still
running; it is not a failure.

## Change and publish

The normal release command rejects a dirty checkout:

```powershell
pwsh .\scripts\windows\publish-stock-release.ps1
```

Only when intentionally snapshotting reviewed, uncommitted work may an operator
use:

```powershell
pwsh .\scripts\windows\publish-stock-release.ps1 -AllowDirty
```

Publishing runs the PowerShell lifecycle tests, all backend unit tests, frontend
type checking and the production frontend build. It snapshots tracked and
non-ignored untracked source, the Python virtual environment, adapter runtime
dependencies and frontend `dist`; writes a release manifest and SHA-256 file
inventory; stops the old runtime; switches the `current` junction; starts both
scheduled services; and verifies local and remote endpoints. Failed activation
automatically attempts to restore the previous release.

Do not pass `-SkipTests` for a production promotion.

### The shared-peer tunnel is not restarted unless it has to be

Publishing used to stop `trading-hareness-shared-peer-tunnels` and re-run
`install-shared-tunnel-task.ps1` every time. That dropped the owner->peer
reverse SSH tunnel for roughly 15 seconds and reset every peer database
connection through it (measured 2026-09-18: 21/23/5 PostgreSQL client resets at
19:42/20:50/21:35) even when no tunnel code had changed.

Both `publish-stock-release.ps1` and `switch-stock-release.ps1` now ask
`Resolve-StockTunnelReinstallPlan` (in `scripts\windows\stock-release-management.psm1`)
*before* stopping anything. The tunnel is left completely alone only when all
of the following hold:

- every tunnel-affecting file is byte-identical between the new release and
  **both** trees a skip has to answer for: the release the tunnel was **last
  installed from** and is therefore still executing (`release-state.json`'s
  `tunnel_release`), **and** the `current` junction's tree, which is what the
  tunnel's *next relaunch* will start from. The registered task action is
  `<PlatformRoot>\current\...`, so `tunnel_release` means "last installed from",
  never "will run from next"; the two diverge as soon as one publish skips, and
  comparing against only one of them is unsound. When the tunnel's supervised
  run started at or after the active release was published the live process
  demonstrably came from `current`, and the gate refreshes the pin to the active
  release (`tunnel_release_pin_refreshed` / `tunnel_release_pin_reason` in the
  plan and in the skip event).
  The file list is the transitive closure of the execution chain: task action →
  `stock-background-host.exe` build inputs → `start-shared-tunnels.ps1` → its
  `Import-Module` targets → `Start-RuntimeSupervisor` →
  `supervise-runtime-process.ps1` → its `Import-Module`/`Add-Type` targets.
  `Get-StockTunnelExecutionChainFile` re-derives it by parsing that chain and
  `test-stock-release-safety.ps1` asserts the declared list *equals* the parsed
  one, so a new import anywhere on the chain fails the test instead of silently
  leaving the gate under-detecting. The parser also follows expandable strings
  (`"$PSScriptRoot\x.psm1"`) and **throws** on a variable it cannot resolve
  statically, so an invisible chain element is loud rather than silent.
  `stock-background-host.exe` is not hashed, because csc.exe recompiles it
  non-deterministically on every publish; its tracked build inputs are hashed in
  its place, and `build-background-task-host.ps1` writes
  `scripts\windows\bin\stock-background-host.build.json` (input SHA-256s +
  output length) which the gate hashes instead of bare presence, so a truncated
  or stale executable cannot pass as "present". A release published before that
  receipt existed reports `present` and both sides degrade to presence;
- the SHA-256 of the resolved owner-tunnel SSH target
  (`Resolve-OwnerTunnelSshTarget`'s destination plus connection arguments, i.e.
  the `OWNER_TUNNEL_SSH_*` values in `config\runtime.env`) matches the one
  recorded at the last real reinstall, so rotating the key, host or port forces
  a reinstall even though no release byte changed;
- the PowerShell host baked into the registered action matches the one
  `Get-InstalledPowerShell` resolves now. On a host with no MSI PowerShell that
  is a version-pinned
  `C:\Program Files\WindowsApps\Microsoft.PowerShell_<version>_x64__...\pwsh.exe`,
  which the Store deletes when it updates PowerShell, and only a reinstall
  re-resolves it;
- the task principal (`LogonType` + `UserId`) matches the one this publish would
  register. `-TaskLogonType` is S4U when elevated and Interactive otherwise;
  `switch-stock-release.ps1` now derives it the same way instead of leaving
  `install-shared-tunnel-task.ps1`'s own `S4U` default in place;
- `tunnel_release` names a release that is still on disk and still inside a
  retention keep set derived from the release directories plus the
  `{active, previous}` pins **only**. Deriving it that way (rather than from
  `Get-StockReleaseRetentionState`, which pins `tunnel_release` out of the same
  state file and would always answer "retained") also bounds how far behind
  `current` a chain of skips may leave the live tunnel: roughly
  `Max(2, RetainCount) - 1` consecutive skips before a reinstall is forced;
- the scheduled task state is `Running`, its registered action (`Execute` plus
  every `.ps1` in `Arguments`) resolves under `<PlatformRoot>\current\`, **and**
  every absolute path the action names still exists on disk
  (`task_action_path_missing` otherwise). A task left pointing at the F:
  development checkout by a manual `install-shared-tunnel-task.ps1` run is only
  healed by a reinstall, so skipping is refused there;
- `G:\StockPlatform\logs\runtime\shared-peer-tunnels.current.json` says
  `status: healthy`. That field records the **last verified install**, not live
  health; the remote probe below is what speaks for the tunnel's current state;
- the same remote probe `install-shared-tunnel-task.ps1` uses returns HTTP 200.

Anything else — a changed file or SSH target, a stopped or misplaced task, a
non-healthy state file, a non-200 probe, an unknown/pruned `tunnel_release`, or
an error while evaluating the gate — keeps the old unconditional stop +
reinstall.

After activation, the post-switch shared-runtime verification decides whether a
skip stands. `Wait-ProductionHealth` deliberately downgrades a failed
`verify-shared-runtime.ps1` to `shared_runtime = degraded` so a lightServer
outage cannot roll back a healthy local release — but when the gate spared the
tunnel, a degraded result now **reinstalls the tunnel and verifies again**
before the publish returns success (`shared_peer_tunnel =
reinstalled_after_degraded_verification`, or
`reinstall_after_degraded_verification_failed` when that repair reinstall itself
threw and nothing was in fact reinstalled);
`switch-stock-release.ps1` does the same around its own
`verify-shared-runtime.ps1` call. Only a skip that survived activation, health
verification *and* the `release-state.json` write — i.e. written after
`$activated = $true`, past every path that can still roll back — writes the
`tunnel_reinstall_skipped` runtime event, so grepping `lifecycle-<date>.jsonl`
for it cannot produce a false positive from a publish that later rolled back and
reinstalled after all. The publish/switch result and `release-state.json`'s
`last_verification` record `shared_peer_tunnel` as `reused_without_reinstall`,
`reinstalled`, `reinstalled_after_degraded_verification` or
`reinstall_after_degraded_verification_failed`.

After a skip the tunnel's supervisor, background host and `ssh` client keep
running out of the release they were **last installed from** until their next
real restart (a tunnel-code change, a tunnel fault, the task's 2-minute
supervising trigger after a drop, a logon, or a reboot) — and that restart
**relaunches from `current`**, because that is what the registered action names.
The last-installed-from release is **pinned**: every real reinstall writes
`tunnel_release` into `release-state.json`, `Get-StockReleaseRetentionPlan` keeps
it unconditionally alongside the active and previous releases (ignoring a
phantom pin that names no existing directory, so it cannot consume a retention
slot), and the gate refuses to skip when it is unknown, gone or outside the
independently-derived keep set. A skip therefore can never outlive its own
directory — but it is still one more reason never to delete a release directory
by hand. `get-stock-release-status.ps1` shows it as the top-level
`shared_peer_tunnel.last_installed_from` plus a per-release
`shared_peer_tunnel_installed_from` flag, alongside
`shared_peer_tunnel.relaunches_from` (the `current` junction).

Because `tunnel_release` is only written by a real reinstall, the **first**
publish after this change always reinstalls (`tunnel_release_unknown`); the one
after that is the first that can skip.

## Rollback

List releases:

```powershell
pwsh .\scripts\windows\get-stock-release-status.ps1
```

Switch to a retained release:

```powershell
pwsh .\scripts\windows\switch-stock-release.ps1 -ReleaseId '<release-id>'
```

The switch command stops both scheduled services, changes the junction, starts
the selected release and reruns shared-runtime verification. Never copy files
over `G:\StockPlatform\current` and never recursively delete a release by hand.
The tunnel reinstall gate described above applies here too, on the forward
switch and on the automatic revert, so a rollback between two releases that do
not differ in tunnel code leaves the reverse SSH tunnel untouched.

## Evidence and diagnosis

### Background tasks must never interrupt the desktop

The shared tunnel incident on 2026-09-05 was a two-minute restart loop: the
daily repetition boundary stopped a task while its detached SSH survived,
then interactive PowerShell launches flashed consoles and failed on port 15432.
Do not restore a direct `pwsh.exe` scheduled action, even with `-WindowStyle Hidden`.
Both runtime tasks now use `stock-background-host.exe` (compiled as Windows GUI,
not a console application) with `CreateNoWindow` at the **first** child launch;
WScript hidden-style launch is not accepted for these runtime tasks. Supervised
processes use `CreateNoWindow`, a per-service exclusive lock and a kernel
kill-on-close job. Shared SSH also exits when its owning scheduled parent dies.
`StopAtDurationEnd` must be false; a recovery tick is not a lifetime limit.

Before restoring a task after a popup incident, run
`scripts/windows/build-background-task-host.ps1` (the Windows .NET Framework
compiler builds the GUI executable under ignored `scripts/windows/bin`), then
`scripts/windows/tests/test-background-process.ps1`, `test-runtime-isolation.ps1`
and `test-hidden-scheduled-task.ps1`. The latter creates a disposable **real**
Task Scheduler job, crosses a repeat/end boundary, triggers a duplicate, stops
the parent, and records window-show/foreground events without manipulating
the desktop. Evidence is under `G:\StockPlatform\logs\runtime\acceptance`.
Then verify the actual SSH/API path and at least two real recovery ticks.
All recurring native health/bootstrap commands must go through
`Invoke-ConsoleFreeCommand`, including `pg_isready`, `pg_ctl`, SSH and Python.
`CreateNoWindow` on a parent alone is not a contract for its later native children.
For this kind of repair publish with `-KeepStoppedOnFailure`; failed activation
must leave tasks disabled instead of silently restoring a noisy older launcher.

Dashboard startup treats PostgreSQL readiness as a state machine: it retries readiness, checks `pg_ctl status`, waits for an already-running server, and starts PostgreSQL only when `pg_ctl` confirms it is stopped. The reverse dashboard tunnel owns reserved remote port `15680`; after remote health failure it stops the supervised local tunnel, waits for normal teardown, and only then clears that exact stale listener before restarting. Each decision and failure is recorded below.

Every owner API, dashboard adapter, dashboard tunnel and shared peer tunnel
launch has a unique run ID. Inspect the bounded status output first:

```powershell
pwsh G:\StockPlatform\current\scripts\windows\get-stock-runtime-status.ps1
```

Detailed evidence is under:

```text
G:\StockPlatform\logs\runtime\lifecycle-YYYY-MM-DD.jsonl
G:\StockPlatform\logs\runtime\<service>.current.json
G:\StockPlatform\logs\runtime\services\<service>\YYYY-MM-DD\<run-id>.*
```

An unmarked child exit is `unexpected_exit`; an operator stop or controlled
release switch is `stopped`. Logs from a prior run are never truncated by a new
run.

## Repository reading order

1. `AGENTS.md` — safety, ownership and test rules.
2. `docs/AGENT_HANDOFF.md` — live deployment and takeover procedure.
3. `docs/ARCHITECTURE.md` — module boundaries.
4. `docs/ARCHITECTURE_INDEX.md` — generated ownership index.
5. `docs/STOCK_BRAIN_MIGRATION.md` — old/new system boundary.
6. `docs/SHARED_PEER_RUNTIME.md` — owner, lightServer and collaborator topology.
7. `docs/SHARED_STOCK_DATA_API.md` — complete read-only stock API contract.

Legacy `stock-brain-*` scheduled tasks were allowed to coexist only during the
cutover observation window.  After the 2026-09-15 archive/readback acceptance,
their exact task definitions are archived and the disabled definitions are
removed with `remove-legacy-stock-brain-tasks.ps1`.  A release operation must
never recreate them; only an explicit historical-recovery procedure may do so.
# 2026-09-10 cutover in progress

Read [CUTOVER_20260910.md](CUTOVER_20260910.md) before operating this host. The legacy stock-brain jobs are archived and removed, and must not be recreated automatically. Production recovery and historical import have separate receipts; neither unit tests nor successful publication mean full acceptance.
# News-delivery cadence update — 2026-09-15

News acquisition/application now has its own console-free Windows task:
`trading-hareness-event-research-delivery`. Public targets are exchange trading
days 09:00/12:00/22:00 China time; closures use last trading day 22:00,
next trading eve 22:00, next trading day 09:00. Reviewed SSE annual calendar
currently covers 2026; unknown years fail explicitly, no weekday fallback.
Starts five minutes early, bounded retry,
PG slot receipts/advisory lock, no broker UI and no Codex-token dependency.
Uses the existing Longhu <=300 wrapper and configured official DeepSeek API.
See `docs/EVENT_RESEARCH.md` for failure/freshness semantics and acceptance.
`scripts/windows/test-event-delivery-live.ps1` invokes the actual registered
silent host with a separate manual key, not a fake future successful slot.
Live news overlays auto-refresh without rewriting frozen scan/report cutoffs.

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

Authoritative PostgreSQL data
  G:\StockPlatform\data\postgresql16

Private runtime configuration
  G:\StockPlatform\config\runtime.env

Runtime logs and lifecycle evidence
  G:\StockPlatform\logs\runtime

Peer credentials, exports and staging
  G:\StockPlatform\peer
```

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

Legacy `stock-brain-*` scheduled tasks can coexist during migration. They are
not proof that the trading-hareness production runtime is unhealthy, and they
must not be silently deleted as part of a release operation.
# 2026-09-10 cutover in progress

Read [CUTOVER_20260910.md](CUTOVER_20260910.md) before operating this host. The legacy stock-brain jobs are intentionally paused and must not be restarted automatically. Production recovery and historical import have separate receipts; neither unit tests nor successful publication mean full acceptance.
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

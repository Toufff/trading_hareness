# Agent Handoff Runbook

This is the authoritative entry point for an agent taking over the running
Windows research platform. Read it together with the repository `AGENTS.md`;
do not infer production state from the development checkout.

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

# trading_hareness

A local market-research platform for A-share equities. It ingests analyst
media and market data, keeps a point-in-time evidence trail, generates
research candidates and a personal decision brief, and reads a broker
position through a read-only bridge. It does not connect to a broker order
path; no route places, amends or cancels an order.

## Current production topology (Windows, `G:\StockPlatform`)

Production runs on a Windows workstation. The authoritative PostgreSQL
cluster, raw evidence, canonical market data, research workers and the
broker read-only bridge all live under `G:\StockPlatform` on the local data
disk; the Git checkout on `F:\AIWorkflow\trading_hareness` is development
only and is never itself the running service.

```text
Development checkout
  F:\AIWorkflow\trading_hareness

Immutable production releases
  G:\StockPlatform\releases\<release-id>\app
Active production junction
  G:\StockPlatform\current
Authoritative PostgreSQL data
  G:\StockPlatform\data\postgresql16          (127.0.0.1:55432)
Private runtime configuration
  G:\StockPlatform\config\runtime.env
Runtime logs and lifecycle evidence
  G:\StockPlatform\logs\runtime
Peer credentials, exports and staging
  G:\StockPlatform\peer
```

| Service | Local endpoint | Remote relay (lightServer) |
|---|---|---|
| quant owner API | `127.0.0.1:5681` | `127.0.0.1:15681` |
| dashboard adapter (feishu-adapter) | `127.0.0.1:5680` | `127.0.0.1:15680` |
| PostgreSQL | `127.0.0.1:55432` | `127.0.0.1:15432` |
| collaborator (peer) API | remote only | `127.0.0.1:15682` |

The scheduled production services execute only from `G:\StockPlatform\current`
(an immutable release junction). lightServer is an optional relay/reverse
proxy; it never becomes the authoritative database or a second writer. An
optional reviewed collaborator ("peer") can run the same research code
against a rootless-Docker sandbox that only reaches the owner's data through
a loopback-only SSH tunnel, and cannot write to the owner's control plane by
default (see [`docs/SHARED_PEER_RUNTIME.md`](docs/SHARED_PEER_RUNTIME.md)).

**Anyone taking over this deployment must start with
[`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md)** — it is the authoritative,
step-by-step takeover runbook (first-five-minutes checks, publish/rollback
commands, evidence locations). Read it together with `AGENTS.md`. Do not
infer production state from this development checkout.

Further reading, in the order `AGENT_HANDOFF.md` itself recommends:

1. [`AGENTS.md`](AGENTS.md) — safety, ownership and test rules.
2. [`docs/AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md) — live deployment and takeover procedure.
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module boundaries.
4. [`docs/ARCHITECTURE_INDEX.md`](docs/ARCHITECTURE_INDEX.md) — generated ownership index (do not hand-edit).
5. [`docs/STOCK_BRAIN_MIGRATION.md`](docs/STOCK_BRAIN_MIGRATION.md) — old/new system boundary, contracts and cutover acceptance.
6. [`docs/SHARED_PEER_RUNTIME.md`](docs/SHARED_PEER_RUNTIME.md) — owner, lightServer and collaborator topology.
7. [`docs/SHARED_STOCK_DATA_API.md`](docs/SHARED_STOCK_DATA_API.md) — complete read-only stock API contract.
8. [`docs/README.md`](docs/README.md) — index of every document under `docs/`, grouped by runbook / design / research-log.

The Windows `stock-brain` cutover, facts-only import boundary, actual-portfolio
contracts and personal decision dashboard are documented in
[`docs/STOCK_BRAIN_MIGRATION.md`](docs/STOCK_BRAIN_MIGRATION.md). The migration
never exposes a broker order path and does not import legacy action-card plans.

The quant API defaults to `http://127.0.0.1:5681`. The personal decision page
separates the settled market review, exact CITIC holdings, qualified
conditional buys and the human-readable research audit. See
[`docs/STOCK_BRAIN_MIGRATION.md`](docs/STOCK_BRAIN_MIGRATION.md) for the
contracts, endpoints and cutover acceptance criteria, and
[`docs/STRATEGY_LOOP_V1.md`](docs/STRATEGY_LOOP_V1.md) for the current
strategy/outcome methodology (both carry a 2026-09-02 methodology revision
note — historical replay numbers from before that date are not comparable to
numbers produced after it).

Analyst opinions are synced from remote, already-parsed report/message
archives; the local `feishu-adapter` also relays Feishu group messages and
media into that same ingestion pipeline (webhooks, manual relay UI at
`/relay`, and a workbench at `/workbench` — the day-to-day behavior is
described in the archived [`docs/legacy/MACOS_COLIMA_ERA.md`](docs/legacy/MACOS_COLIMA_ERA.md)
runbook and is largely unchanged; only *how the adapter process is started*
changed with the Windows migration).

## New environment variables introduced this hardening round

The table below summarizes every environment variable newly introduced (or
whose default changed) by the 2026-09-02 audit remediation work packages.
Existing variables not listed here are unchanged; see
[`.env.example`](.env.example), [`OPERATIONS.md`](OPERATIONS.md) and
[`DEPLOYMENT.md`](DEPLOYMENT.md) for the rest.

### `feishu-adapter` (Node)

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_OPERATOR_KEY` | none — **required** | Operator credential. Every mutating route (`POST`/`PUT`/`DELETE`/`PATCH` under `/api/` plus `/manual-relay`, `/reconcile`, `/n8n-status`, `/n8n-error`) requires this value in the `X-Dashboard-Key` header, compared with `crypto.timingSafeEqual`. **The adapter process now fails to start if this is unset**, unless `DASHBOARD_ALLOW_UNAUTHENTICATED=1` is explicitly set. |
| `DASHBOARD_ALLOW_UNAUTHENTICATED` | unset | Set to `1` to explicitly bypass the fail-closed startup check above. Prints a warning; intended for temporary local debugging only, not for a standing deployment. |
| `DASHBOARD_HOST` | `127.0.0.1` (was `0.0.0.0`) | The adapter now binds loopback-only by default; a wider listen address must be set explicitly. |
| `MANUAL_RELAY_MAX_FILE_BYTES` | `209715200` (200 MB) | Per-file cap for the `/manual-relay` multipart form. |
| `MANUAL_RELAY_MAX_TOTAL_BYTES` | `1073741824` (1 GB) | Total media cap per `/manual-relay` request. |
| `FEISHU_HTTP_TIMEOUT_MS` | `20000` | Idle-timeout (axios) applied to `Lark.defaultHttpInstance`, covering every `larkClient.*`/`WSClient` call. Does not truncate an actively-streaming large media download. |
| `INGESTION_DB_CONNECT_TIMEOUT_MS` | `10000` | Ledger PostgreSQL pool `connectionTimeoutMillis`. |
| `INGESTION_DB_STATEMENT_TIMEOUT_MS` | `30000` | Ledger PostgreSQL pool `statement_timeout`/`query_timeout`. |
| `INGESTION_MAX_RETRY_ATTEMPTS` | `20` | Shared maximum retry count for the n8n delivery outbox, the Baidu Pan archive queue and Feishu group-relay message claims; a row past this limit stays terminally `failed` instead of retrying forever. |

### `quant-service` (Python, second-wave hardening)

| Variable | Default | Meaning |
|---|---|---|
| `QUANT_CONTROL_PLANE_WRITES_ENABLED` | `true` | When explicitly set to `false` — as `deploy/shared-peer/compose.yaml` already does for the peer's `quant-research` service — the service is meant to start without opening the provider control-plane write path. Unset (the previous behavior) is unaffected. |
| `QUANT_ALLOW_UNAUTHENTICATED_WRITES` | unset (fail-closed) | When `QUANT_WRITE_API_KEY` is missing, the service is meant to refuse to start unless this is explicitly set; there is no implicit "writes just work with no key" fallback. |

**Implementation status as of this writing:** `deploy/shared-peer/compose.yaml`
already sets `QUANT_CONTROL_PLANE_WRITES_ENABLED=false` for the peer, but
`quant-service/app/main.py` does not yet read either variable — only
`QUANT_WRITE_API_KEY` itself is checked today
(`app/main.py`, the `configured_key = os.getenv("QUANT_WRITE_API_KEY", ...)`
site). Wiring these two switches into `main.py`'s write-path startup checks
is `main.py`-owned and out of scope for whichever work package edits this
file next; see that work package's "needs another work package" section for
the precise diff.

### `scripts/shared-peer` / `scripts/windows` (owner tunnel, PowerShell — see `docs/SHARED_PEER_RUNTIME.md`)

| Variable | Default | Meaning |
|---|---|---|
| `OWNER_TUNNEL_SSH_USER` / `OWNER_TUNNEL_SSH_KEY` / `OWNER_TUNNEL_SSH_HOST` / `OWNER_TUNNEL_SSH_PORT` | unset (falls back to the pre-existing `lightServer1` SSH alias, with a warning) | When all four are set in `runtime.env`, the owner's reverse tunnel scripts log in as a dedicated, restricted lightServer account instead of a root-capable alias. See "Owner bootstrap" in `docs/SHARED_PEER_RUNTIME.md`. |
| `STOCK_BACKUP_ROOT` / `STOCK_BACKUP_MIN_FREE_BYTES` | `<PlatformRoot>\backups` / provider default | Destination and free-space guard for the new `scripts/windows/backup-stock-database.ps1` daily `pg_dump` job. |

## Repository layout

```text
quant-service/       FastAPI research service, PostgreSQL access, Alembic migrations
feishu-adapter/       Node ingestion/relay adapter and dashboard (runs directly via node.exe under Windows)
frontend/             Vue 3 + TypeScript research dashboard SPA
scripts/windows/       Windows platform lifecycle scripts (publish, rollback, backup, watchdog)
scripts/shared-peer/   Owner/peer bootstrap, tunnels and data-migration scripts
deploy/shared-peer/    Peer-side Docker Compose deployment
docs/                  Runbooks, design docs and dated research logs — see docs/README.md
legacy/macos/          Archived macOS/Colima/launchd-era scripts and plists (see docs/legacy/MACOS_COLIMA_ERA.md)
```

`OPERATIONS.md` and `DEPLOYMENT.md` at the repository root document the
`feishu-adapter` relay/workbench behavior and the `quant-service` HTTP
contract respectively; both now point to `docs/AGENT_HANDOFF.md` for how the
platform actually starts and is operated today.

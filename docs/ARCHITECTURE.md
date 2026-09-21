# Quant Research Platform Architecture

This is a local market-research platform.  It does not connect to a broker and
does not submit orders.  Every strategy result is research evidence until its
separate promotion gate is satisfied.

## Runtime map

```text
Feishu / n8n / browser
        |
        v
feishu-adapter (proxy, relay, OAuth and media boundary)
        |
        v
quant-research FastAPI
  routers -> services/orchestrators -> repositories -> PostgreSQL
                                      -> provider adapters -> external sources
        |
        +-> raw -> canonical -> features -> signals -> outcomes
```

The deployable background profiles split this map without changing the HTTP or
research contracts. `intraday_edge` is the single live-polling and Feishu-alert
writer for `intraday_monitor`, fast quote, minute profile, order book and board
flow. `research` owns post-close review and local replay, but never starts those
five polling loops. The edge keeps a bounded PostgreSQL database and streams an
allowlisted, monotonically sequenced evidence-change journal back to the
workstation over a forced-command SSH key. The journal is captured only by an
`intraday_edge` connection profile, so importing an edge row into the research
database cannot echo it back into a new export. The importer is transactional
and deliberately excludes leases, delivery outboxes, recommendations,
credentials and any order-like state. A workstation outage therefore delays
analysis visibility without stopping collection or losing retained evidence.

Both profiles run the same committed source revision. The distinction is
runtime configuration and ownership, not a long-lived server branch: releases
publish a Git SHA and image/source provenance through the loopback health
endpoints, while secret environment files remain outside version control.

Feishu delivery has a separate presentation boundary.  Runtime/database
payloads retain machine field names for replay, while
`intraday_advisory/presentation.py` converts only allowlisted facts into
human-facing Chinese and rejects cards that still contain raw variable names,
booleans, UUIDs or task identifiers.  Holding discipline and recommendation
eligibility are distinct semantics: a non-held recommendation may be paused or
invalidated, but must never receive a reduce/exit instruction.  Incomplete
quote or core-index coverage fails closed before a model is called or a
trading-style report is delivered.

`quant-service/app/main.py` is the composition root by design: it owns
application lifespan, dependency assembly and router registration. As of the
2026-09 audit it is also, in practice, a large historical compatibility
surface — roughly 1,500 lines of business logic and direct SQL that predate
the current router/repository split (a full strategy orchestration, ad hoc
`INSERT`/`UPDATE` statements, an unbounded signal-attribution loop, two
`while True` polling loops), plus 16 `*_legacy` aliases and about 110
"compatibility" forwarding functions with no remaining production caller.
This is a known, being-unwound state, not the intended architecture: each of
those blocks is slated to move into its already-existing counterpart module
(for example the xiaojie leader-flow runtime, the daily control plane, the
intraday watchlist service, intraday replay). **New behaviour must never be
added here** — it belongs in a focused module, then is injected from
`main.py`; production modules must not import `app.main`.

The owner PostgreSQL cluster underneath this map is itself tiered. The cluster —
all tables, indexes, WAL and temp files — lives on an NVMe hot tier addressed by
`PGDATA_DIR` (production `F:\StockPlatformDB\postgresql16`) under a 500 GB soft
budget, while rows older than the hot window (365 days for five append-only
evidence tables) are moved nightly into `quant.<table>_cold` twins in the
`stock_cold` tablespace on the G: HDD, unioned by a `quant.<table>_all` view for
the rare full-history query. The platform root, the cold tablespace and every
backup stay on G:. Application code reads the hot table only; the twins carry no
foreign keys and no triggers and are an operations surface. The layout, the space
policy, the 04:00-08:00 maintenance window, the backup chain and the
migration/rollback runbook are defined in
[`OWNER_DATABASE_STORAGE.md`](OWNER_DATABASE_STORAGE.md), which is the
authoritative document for anything that touches database placement.

The owner reaches the lightServer peer over **two** independent reverse SSH
tunnels, not one. `trading-hareness-shared-peer-tunnels` carries the intraday
request path (remote 15432 -> local 55432, remote 15681 -> local 5681) and
`trading-hareness-shared-peer-batch-tunnel` carries bulk/backfill/COPY traffic
(remote 15433 -> local 55432, peer-side 5433). They are separate scheduled
tasks, runtime services, state files and lock files, and
`-o ControlMaster=no -o ControlPath=none` keeps them on separate TCP
connections so a bulk transfer cannot take the intraday path's congestion
window. Both are installed by one fan-out,
`scripts/shared-peer/install-shared-tunnel-tasks.ps1`, which publish and switch
call: the intraday tunnel's failure still fails the install, the batch
tunnel's is reported and recorded but never fails a release. The publish
reinstall gate judges **both** tasks, so a skip means both are Running, healthy
and rooted under `current`. See
[`SHARED_PEER_RUNTIME.md`](SHARED_PEER_RUNTIME.md) and
[`PEER_BATCH_TUNNEL_ROLLOUT.md`](PEER_BATCH_TUNNEL_ROLLOUT.md).

`quant.canonical_bars_daily.adj_factor` and `quant.market_bars_daily.adj_factor`
are cumulative corporate-action factors, never a same-day identity placeholder:
a vendor without corporate-action history emits no factor row at all and the
date is filled out of band by `scripts/adjustment-factor-maintenance.py sync`,
which derives the factor from the licensed longhu kline (no tushare call).
[`ADJUSTMENT_FACTOR_SEMANTICS.md`](ADJUSTMENT_FACTOR_SEMANTICS.md) is
authoritative for that lane, including the one-time repair
(`scripts/adjustment-factor-maintenance.py repair`) of the identity factors
already written to canonical bars.

Every rule in the table below is enforced by a test, not just documented —
see "Architecture guard tests" in `AGENTS.md` for the current list
(`tests/test_router_composition.py`, `tests/test_migration_contracts.py`,
`tests/test_async_database_boundaries.py`, `tests/test_storage_tier_policy.py`,
`tests/test_instrument_writer_lock_order.py`,
`tests/test_daily_bar_caller_lock_order.py`,
`tests/test_adjustment_factor_semantics_guard.py`,
`tests/test_peer_batch_tunnel_deploy.py`).
Adding a new architectural rule without an accompanying test is incomplete.

## Ownership boundaries

| Concern | Location | Rule |
|---|---|---|
| HTTP request validation | `app/routers/` | Router functions validate and delegate; no provider crawling in a read route. |
| Provider transport | `app/*provider*.py`, `app/http_clients.py` | Reuse lifecycle clients and record availability. |
| Evidence semantics | `app/platform/evidence_contracts.py` | Every normalized source declares provider, capability, scope, coverage semantics and decision eligibility before strategies consume it. |
| Data placement and replay | `app/platform/data_product_registry.py` | Every strategy/runtime dataset declares time semantics, local tier, immutable cloud format, partition keys and replay role. Cloud copies never become direct decision inputs. |
| Persistent projections | `app/*_repository.py`, `app/*_read_model.py` | Bound result sets; async dashboard reads use `AsyncDatabase`. |
| Timing and recovery | `app/*_scheduler.py`, `app/runtime_tasks.py`, `app/*_runtime.py` | Durable leases, idempotent run keys and explicit retry windows; runtime adapters bind scan I/O and lease ports without embedding transactional closures in the ASGI root. |
| Runtime ownership | `app/platform/runtime_task_registry.py` | Each leased task declares one owner profile, expected cadence, upstream capabilities and retained evidence datasets; startup rejects an undeclared or missing task factory. |
| Rules and research | `app/*_rules.py`, `app/*_research.py` | Keep inputs/outputs explicit and test without HTTP or database state. |
| Strategy contracts | `app/platform/strategy_registry.py` | Every strategy declares its model/input contract, runtime owner, retained evidence and `live_effect=none`; startup rejects missing or mismatched materialized model versions. |
| Decision products | `app/short_term_lanes/`, `app/recommendation_pool/` | Nine-lane screening, primary-source company review and formal recommendation are separate persisted states. Retired G0--G7 dossiers remain migration history only and have no active route or UI projection. |
| Instrument registration | `app/instrument_registry.py` | Every write to `quant.instruments` goes through this module in one sorted, batched statement, and every per-bar caller iterates `in_instrument_lock_order(bars)`, so concurrent ingestion transactions take the same ascending row-lock order. |
| Adjustment factors | `scripts/adjustment-factor-maintenance.py`, `app/longhu_adjustment_factors.py`, `docs/ADJUSTMENT_FACTOR_SEMANTICS.md` | Bar tables take a cumulative corporate-action factor or nothing; factors are derived from longhu (`longhu_qfq_derived`), a vendor placeholder is stored as raw evidence and never promoted. |
| Owner -> peer tunnels | `scripts/shared-peer/shared-tunnel-profiles.psm1`, `install-shared-tunnel-tasks.ps1` | Two profiles (intraday, batch) with their own tasks, services, state and lock files, installed together and judged together by the publish reinstall gate. Batch is an optimization and can never fail a release. |
| Schema | `migrations/versions/` | New production schema changes use Alembic only. |
| Physical placement | `scripts/database-storage-tiers.py`, `docs/OWNER_DATABASE_STORAGE.md` | Hot tier (NVMe, `PGDATA_DIR`, 500 GB budget) vs `stock_cold` tablespace on G:. A new large evidence table registers a tier policy; `app/` never reads a `*_cold` twin or a `*_all` view. |
| Legacy bootstrap | `app/database.py` | Disabled by default; only an explicit recovery operator may enable it. |
| Frontend transport | `frontend/src/api/http.ts` | All JSON responses produce a readable non-JSON proxy error. |
| Frontend lifecycle | `frontend/src/composables/` | Timers and subscriptions are owned and stopped by the mounting shell. |
| Frontend feature UI | `frontend/src/components/`, `frontend/src/views/` | New dashboard surfaces must not grow `App.vue`. |

## Agent entry sequence

1. Read `GET /api/v1/agent/context`, its evidence/task contract catalogs, and the latest durable automation receipt.
2. Read the owning router, service, repository, migration and targeted test.
3. Preserve point-in-time boundaries: `stated_at` is replay evidence;
   `strategy_available_at` is the only strategy eligibility time.
4. Make one bounded change and add a focused test in the same domain.
5. Run backend tests, adapter tests, frontend API/type/build checks and
   `git diff --check`.
6. Verify mounted OpenAPI and `/health`; a source-only pass is not a runtime
   acceptance.

Cross-domain work starts from an identified commit and ends in a new coherent
commit before another task begins.  Production normally publishes only a clean
checkout.  A manifest-backed dirty release is reserved for emergency diagnosis
and never becomes an implicit development baseline.  Release evidence records
the source commit, release id and live readback independently from unit/build
results.

## Data and decision gates

- Missing/stale provider data, incomplete sector membership and insufficient
  statistical samples fail closed.
- The daily control plane applies to listed equities only.  Index benchmark
  rows are valid market context but do not carry equity `adj_factor` or
  `stk_limit` controls.
- `raw -> canonical -> features -> signals -> outcomes` is append/evidence
  oriented.  Replay outcomes never change live weights.
- Each layer is archived as immutable, manifest-addressed evidence. Research
  restores cloud partitions into staging and validates schema, hash, row count
  and point-in-time fields before use; strategies never query cloud objects in
  the live decision path.
- Network loss keeps local loops alive.  Durable cursors, leases and run keys
  resume work after recovery without replaying completed work.

## Frontend ownership

`frontend/src/App.vue` is the shell only: navigation, lazy view registration
and shell-owned dialogs. `useDashboardWorkspace.ts` owns cross-domain polling,
SSE lifecycle and research mutations; feature composables such as
`useFeishuRelayWorkspace.ts` own their status, CRUD and operation state.
Feature views receive a scoped typed injection contract where available rather
than an unbounded dashboard record.
Research tabs are independently lazy-loaded from
`frontend/src/views/research/`; relay monitoring and Feishu workbench are their
own views. New UI must join one of these owners instead of growing App.vue.

The frontend has unit coverage for the JSON transport and timer lifecycle, plus
a browser smoke test for the mounted research shell. API types remain generated
from the mounted OpenAPI contract.

The legacy DDL retained in `app/database.py` is recovery-only.  It remains
isolated behind `QUANT_LEGACY_SCHEMA_BOOTSTRAP`; new migrations must never edit
that bootstrap SQL.

# Repository agent guide

## Scope

This repository runs a market-research platform, not an order execution system.
All analyst scoring, regression, calibration, pattern mining and recommendations
are evidence-only until an explicit promotion record says otherwise. Never add a
provider response directly to a live threshold or order path.

## Change map

- `quant-service/app/routers/`: HTTP boundary and request validation.
- `quant-service/app/*_repository.py`: database read/write projections.
- `quant-service/app/*_scheduler.py`: timing, retry windows and idempotency only.
- `quant-service/app/*_rules.py` / `*_research.py`: pure or research-only rules.
- `quant-service/app/main.py`: composition root by design, but historically
  carries roughly 1,500 lines of leftover business logic, direct SQL and
  compatibility forwarding (`*_legacy` aliases, duplicated status payload
  helpers) that predates the router/repository split and is being unwound
  incrementally. Read it as "legacy compatibility surface being reduced",
  not as the intended shape. **New behaviour must never be added here** — it
  belongs in a focused router/repository/rules module and is only wired up
  from `main.py`.
- `quant-service/migrations/versions/`: all production schema changes (Alembic).
- `frontend/src/App.vue`: current Vue dashboard; keep API calls typed and label
  research-only/replay-only values visibly.
- `frontend/src/api/http.ts`: shared JSON/error transport; do not duplicate
  browser response parsing in a feature panel.
- `frontend/src/composables/`: lifecycle-owned polling/subscriptions.
- `frontend/src/components/`: focused feature panels; new UI must not expand
  the root dashboard shell.
- `feishu-adapter/index.mjs`: browser/API proxy; route mappings need separate GET
  and POST entries.
- `quant-service/app/security.py`: shared write-boundary primitives; keep this
  module framework-light so HTTP contract tests can import it without startup.

## Time and data rules

- Exchange dates and session windows use `Asia/Shanghai`; persisted event times
  are timezone-aware UTC values.
- Prefer analyst `stated_at` only for replay evidence; `strategy_available_at`
  is the point-in-time eligibility boundary.
- Fail closed on missing bars, stale providers, incomplete sector mappings and
  insufficient samples. Do not invent Top10s, prices or regression coefficients.
- Keep author replay outcomes separate from strategy-available outcomes.
- Application code never reads a `*_cold` twin or a `*_all` view. Those are the
  storage tier's operations surface: the twins carry no foreign keys and no
  triggers, so a router reading one bypasses every referential guarantee the hot
  table has. A query that genuinely needs the full history goes through the
  `quant.<table>_all` view from an operations script, never from `app/`.
- A new large, append-only evidence table must register a tier policy in
  `scripts/database-storage-tiers.py` (and in the policy table of
  `docs/OWNER_DATABASE_STORAGE.md`, which is guard-tested against it). An
  unregistered table grows against the hot-tier budget until the space policy
  starts taking history away from the tables that did register.

## Agent workflow

1. Read `docs/AGENT_HANDOFF.md` and inspect the active release before changing a
   running Windows deployment.
2. Read the relevant module, router, migration and existing test before editing.
3. Add a pure-function test first, then a repository/HTTP test when a route or
   persistence path changes.
4. Use `apply_patch`; do not rewrite generated artifacts or expose `.env` values.
5. Run `docker compose exec -T quant-research python -m unittest discover -s tests -q`,
   `cd frontend && npm run typecheck && npm run build`, and `git diff --check`.
6. For a scheduler change, verify database rows, latest status endpoint and one
   real adapter request; a unit test alone does not prove the published route.
7. Run `node scripts/verify-api-contract.mjs` after adding or renaming a route;
   it checks the running OpenAPI document instead of trusting a source-only map.
8. Run `cd frontend && npm run api:generate` after an intentional API contract
   change; `npm run api:check` verifies the checked-in generated type is current.
9. Read `docs/ARCHITECTURE.md` before a cross-domain change; it is the concise
   ownership map, while this file remains the operational checklist.
10. Read `docs/OWNER_DATABASE_STORAGE.md` before touching the database layout,
    backups, scheduled maintenance or any table listed in the tier policy. The
    owner cluster is split across a 500 GB NVMe hot tier (`PGDATA_DIR`, production
    `F:\StockPlatformDB\postgresql16`) and a `stock_cold` tablespace on G:; the
    data directory, the backup exclusions, the maintenance window (04:00-08:00)
    and the role timeouts are all defined there, not here.

## Version-control and release discipline

- Inspect `git status --short` before editing.  If unrelated work is present,
  preserve it and establish an explicit checkpoint before a cross-domain
  refactor; do not silently mix several tasks into one unreviewable diff.
- Commit each coherent, tested change separately.  A repository with completed
  implementation work must not be left dirty while a new task starts.
- Run a staged secret scan and `git diff --cached --check` before every commit.
  Agent/browser state, generated test output, runtime configuration and real
  credentials never enter Git.
- A production release normally comes from a clean commit.  `-AllowDirty` is an
  emergency, manifest-backed diagnostic escape hatch, not the normal publish
  path; the dirty snapshot must be committed or discarded before more work.
- Every deployment handoff records the source commit, release id, targeted test
  result, full-suite result and live readback result.  Source tests, deployment
  and live acceptance are separate claims.
- Never describe a source-only or mocked test as an end-to-end acceptance.

## Architecture guard tests

The convention in this repository is: **every architecture rule above has a
test that fails if the rule is violated**, not just a written statement. Do
not remove or weaken one of these without removing the rule it enforces from
this file and `docs/ARCHITECTURE.md` in the same change:

- `quant-service/tests/test_router_composition.py` — every `build_*_router`
  factory in `app/routers/*.py` must be imported and mounted exactly once by
  `main.py`, and `main.py` must not mount an unknown router.
- `quant-service/tests/test_migration_contracts.py` — the frozen legacy DDL
  (`SCHEMA_SQL`/`PLATFORM_SCHEMA_SQL`) is hashed and pinned; every Alembic
  migration's revision chain must be linear with a single head; every
  `ADD CONSTRAINT` must be guarded for idempotency; every `ALTER TABLE`
  target must already exist in the frozen DDL or an earlier migration.
- `quant-service/tests/test_async_database_boundaries.py` — guards the event
  loop from an accidental direct synchronous database call inside an async
  code path (the async dashboard/read routers must go through
  `AsyncDatabase`, never a blocking transaction on the event loop thread).
- `quant-service/tests/test_repository_workflow_policy.py` — keeps the clean
  commit/release boundary, secret-state ignores and live-acceptance wording in
  the repository contract.
- `quant-service/tests/test_storage_tier_policy.py` — every table in the storage
  tier policy exists in the frozen DDL or a migration and its cutoff column is a
  `timestamptz` of that table; no file under `app/` references a `*_cold` twin or
  a `*_all` view; the policy table in `docs/OWNER_DATABASE_STORAGE.md` lists
  exactly the tables the script acts on.
- `quant-service/tests/test_database_storage_tiers.py` — the storage tier job's
  pure functions and every statement it renders: hot-usage measurement skips
  reparse points (exercised against a real directory junction, because the
  `pg_tblspc` junction points at the cold tier) and reports `pg_wal` separately;
  the effective budget is capped by the volume; the space ratchet is bounded per
  run and stops at `needs_repack` instead of running `VACUUM FULL`; every move
  statement uses explicit column lists (never `SELECT *`, never `ctid`) and
  inserts into the twin before deleting from hot; deadline resolution; the
  schema-drift comparison; the status → exit-code mapping; and the cutoff-index
  set of migration `20260919_0106` pinned against `TIER_POLICY`.

The same convention covers the Windows runtime. These PowerShell guard tests are
standalone (no database, no venv, no scheduled task) and run as
`pwsh -NoProfile -File <test>.ps1`; `publish-stock-release.ps1` executes them
before every release:

- `scripts/windows/tests/test-postgres-storage-tier-wiring.ps1` — the PowerShell
  runner and the Python CLI cannot drift: every `-Command` the runner offers is a
  real subcommand of `scripts/database-storage-tiers.py`; every flag the runner
  passes (including `--deadline` and `--max-seconds`) is a flag the CLI declares;
  the `status` → exit-code map stays 0 (`ok`/`deadline_reached`) / 1 (`partial`,
  `conflicts`, `schema_drift`) / 2 (`degraded`, `failed`) and the runner reports
  it verbatim; the run record stays at `logs\storage-tiers.jsonl`; the initializer
  seeds **no** dump exclusions and `backup-stock-database.ps1` computes them at
  dump time, so a cold twin is only left out when its hot table has an
  incremental chunk chain; and `quant.storage_tier_conflicts` is never a `_cold`
  name, because the nightly dump must keep it.
- `scripts/windows/tests/test-postgres-io-window.ps1` — the pure window function
  `Get-PostgresIoWindowMode` across every boundary.
- `scripts/windows/tests/test-postgres-data-migration-contract.ps1` — the
  data-directory migration keeps its safety order (`Disable-ScheduledTask` →
  graceful `stop-stock-dashboard.ps1` → `Stop-ScheduledTask` backstop, verify the
  copy before switching `PGDATA_DIR`, rename the old directory instead of
  deleting it), recovers the platform from a failure in any step and rethrows,
  copies with `/XJ` while recreating the `pg_tblspc` junctions, and gates
  `-Rollback` behind `-AcceptDataLoss` plus an absolute refusal when the image
  predates the `stock_cold` tablespace.
- `scripts/windows/tests/test-backup-stock-database.ps1` — the computed dump
  exclusion rule itself: chain present/absent, a failed incremental export, and
  an operator override naming a twin whose hot table has no chain (refused and
  reported, never silently honoured).

One Windows test in this family is **not** standalone and is not run by
`publish-stock-release.ps1`: `scripts/windows/tests/test-stock-incremental-backup-drill.ps1`
starts the real PostgreSQL runtime and creates throw-away databases (dropped
afterwards) to prove a nightly dump with exclusions can actually be restored,
including the `no_chunk_chain` skip. Run it by hand after changing the backup or
restore path.

## Review automation

Analyst daily/weekly reviews are materialized by
`analyst_market_review.py`, persisted in `quant.analyst_market_reviews`, and
triggered by `strategy_review_scheduler.py`. The frontend reads them through
`/api/research/analyst-research/reviews/latest`; the regression is descriptive
and has `live_effect=none` until the documented sample gate is met.

For maintenance triage, read `/api/research/agent/context` first and then
`/api/research/automation/runs?task_key=...`. These are secret-free context and
durable execution evidence, not a substitute for source/test inspection.

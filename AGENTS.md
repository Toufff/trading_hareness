# Repository agent guide

## Scope

This repository runs a market-research platform, not an order execution system.
All analyst scoring, regression, calibration, pattern mining and recommendations
are evidence-only until an explicit promotion record says otherwise. Never add a
provider response directly to a live threshold or order path.

## Change map

- `quant-service/app/routers/`: HTTP boundary and request validation.
- `quant-service/app/*_repository.py`: database read/write projections.
- `quant-service/app/instrument_registry.py`: the shared write primitives for
  `quant.instruments` — `ensure_instruments` for bare symbol registration
  (symbol + exchange + source, `ON CONFLICT DO NOTHING`) and
  `ensure_named_instruments` for symbol + display name (`ON CONFLICT DO UPDATE`
  on the name alone). Both take one statement per 5,000-symbol chunk,
  deduplicate client-side and sort ascending. Both return the rows they
  actually wrote, and that return value is the contract: they strip and drop
  blanks, so a caller that writes a child row referencing
  `quant.instruments(symbol)` in the same transaction takes its symbols from
  the return value, never from its own raw input.

  **The rule, which is enforced and not a roster:** every write to
  `quant.instruments` anywhere in this repository either goes through this
  module or is a set-based statement carrying `ORDER BY 1` between itself and
  its `ON CONFLICT` clause, and it spells its table name literally.
  `quant-service/tests/test_instrument_writer_lock_order.py` walks every `.py`
  outside tests and vendored trees — `quant-service` (including
  `database_bootstrap.py` and `entrypoint.py`, which open the same database
  from outside `app/`), `scripts`, `legacy`, `workflows`, `deploy` — reading
  the parsed source rather than the raw text, so it matches the statement
  whatever the case, whitespace or identifier quoting (`quant."instruments"`),
  each match ends with its own string literal instead of borrowing a later
  statement's `ON CONFLICT` clause, and a file that does not parse is reported
  as a failure of that file rather than as an unrelated error. There is no
  allow-list to add a new writer to, so a per-row `VALUES(...) ON CONFLICT`
  loop fails on the day it is written, and so does a table name built at
  runtime in any of its four spellings — `f"INSERT INTO {SCHEMA}.instruments"`
  (column list or not), `"INSERT INTO " + SCHEMA + ".instruments(...)"`,
  `"INSERT INTO %s.instruments(...)" % SCHEMA` and the `.format()` twin — each
  of which would otherwise hide from the search. Do not answer that failure by
  listing the writer somewhere.

  `ORDER BY 1` is the shared ascending lock order, and it is a correctness
  property, not a tidy-output habit: `ON CONFLICT DO UPDATE` row-locks every
  **existing** conflicting row (the whole cross-section on any run after the
  first) while `DO NOTHING` locks only the rows it genuinely inserts, so two
  transactions touching an overlapping symbol set in different orders
  deadlock — three times in the owner PostgreSQL log on 2026-09-18. The sort
  belongs in the SQL even when the array was already sorted in Python: the
  server sort is then free, and it is what makes the property checkable
  without reading each caller's loop. A single-symbol writer sorts one row and
  still carries it, because "big enough to need it" is the judgement call that
  produced three rounds of drift.

  What the rule does not cover, and what still needs judgement: a writer that
  registers symbols in one statement and then re-locks the same rows later in
  the same transaction is ordered only if **both** statements are. And a
  single-symbol writer driven once per item by a caller loop is ordered by
  that loop, not by its own statement, so **the loop sorts**. Both shapes
  exist today and both are covered by a caller-level test rather than by the
  guard, which cannot see them:

  - `trade_discipline.repository.persist_plan`, driven once per plan by
    `scripts/trade-discipline.py:219`. Sorts on `plan.symbol`; pinned by
    `GenerateLockOrderTests` in `quant-service/tests/test_trade_discipline_cli.py`.
  - `daily_bar_repository.upsert_daily_bar`, driven once per bar by four
    multi-symbol callers: `main.persist_daily_bar_batch`, `main.import_bars`,
    `public_market_repository.persist_free_daily` and
    `tushare_normalization`'s per-row fallback. All four iterate
    `daily_bar_repository.in_instrument_lock_order(bars)` — ascending
    `(symbol, trading_date)` — rather than their payload order; pinned by
    `quant-service/tests/test_daily_bar_caller_lock_order.py`, which also
    fails any *new* loop under `quant-service/app` that drives a one-bar
    writer over an unordered iterable — whether the writer is called by a
    bare name or module-qualified (`daily_bar_repository.upsert_daily_bar`).
    That file also pins the two tables `daily_bar_batch_repository` writes in
    one statement, `market_bars_daily` and `canonical_bars_daily`: both are
    `ON CONFLICT DO UPDATE` on `(symbol, trading_date)` and both sort their
    key list, because no caller can fix a batch's order from outside it.
    They sort rather than hoisting one
    `ensure_instruments` call because that primitive cannot carry `industry`,
    the three-valued `is_st`, or the `exchange`/`source`/`updated_at` refresh
    their `ON CONFLICT DO UPDATE` performs; a lock-order fix must not change
    what is stored.

  If you add a caller loop of your own, add its test here too. The list above
  is a map of the two known shapes, not a partition of the writers — the
  partition is the guard test, and it has no allow-list.
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
- `quant-service/app/adjustment_factor_maintenance.py`: the out-of-band
  cumulative adjustment-factor repair lane (04:00-08:00 window), driven by
  `scripts/adjustment-factor-maintenance.py`; never a post-close stage.
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
- **Bar tables never receive a placeholder adjustment factor.**
  `quant.canonical_bars_daily.adj_factor` and `quant.market_bars_daily.adj_factor`
  are cumulative (hfq-style) corporate-action factors: `close * adj_factor` must
  be comparable across dates. A vendor that publishes no corporate-action
  history emits no `adj_factor` rows at all — never an identity `1`, because
  `1.0` is neither NULL nor `<= 0` and therefore sails past every fail-closed
  adjustment consumer while silently yielding an unadjusted series. NULL is the
  honest value for "not fetched yet"; those dates are filled out of band by
  `scripts/adjustment-factor-maintenance.py sync`, invoked automatically twice:
  as the non-gating `adjustment_factors` post-close stage and by the daily
  04:30 `trading-hareness-adjustment-factors` scheduled task. A coverage or
  readiness check must not read a NULL factor as a missing bar. Promotion onto
  a bar requires BOTH halves of
  `tushare_normalization.promotable_adjustment_factor`: a tushare provider AND
  absent/`corporate_action_cumulative` semantics — a vendor that merely omits
  the marker is refused by the provider half. A NULL sitting in the trailing
  gap of a research window is resolved *on read only*, by the one shared rule
  in `app/research_prices.resolve_factors`: carry the last real factor across
  at most five sessions, and only while each carried session's `pre_close`
  still equals the previous close. That flags `adj_factor_carried_forward`,
  which is recorded, never penalised, and never written back to a bar table.
  See `docs/ADJUSTMENT_FACTOR_SEMANTICS.md` (§1.1).
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
11. Read `docs/SHARED_PEER_RUNTIME.md` and `docs/PEER_BATCH_TUNNEL_ROLLOUT.md`
    before touching anything owner -> peer. There are **two** reverse SSH
    tunnels, not one: `trading-hareness-shared-peer-tunnels` (intraday request
    path) and `trading-hareness-shared-peer-batch-tunnel` (bulk/backfill, peer
    port 5433). They are separate tasks, services, state files and lock files,
    they are installed together by
    `scripts/shared-peer/install-shared-tunnel-tasks.ps1`, and the publish
    reinstall gate judges both. A batch tunnel failure is reported and recorded,
    never raised: it must not be able to fail a release. Any file on either
    profile's execution chain must appear in `$script:StockTunnelAffectingFiles`
    (`scripts/windows/stock-release-management.psm1`), or the gate silently
    under-detects a change to it.
12. Read `docs/ADJUSTMENT_FACTOR_SEMANTICS.md` before touching `adj_factor` on
    any bar table, the post-close `adjustment_factors` stage or the 04:30
    `trading-hareness-adjustment-factors` task.

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
  exactly the tables the script acts on; and no tiered table carries a partial or
  expression UNIQUE index, in the frozen DDL or in any migration, because the
  move cannot reason about one (the detecting regexes are themselves pinned
  against known-good and known-bad samples, so a rewrite cannot quietly turn that
  guard into a no-op).
- `quant-service/tests/test_database_storage_tiers.py` — the storage tier job's
  pure functions and every statement it renders: hot-usage measurement skips
  reparse points (exercised against a real directory junction, because the
  `pg_tblspc` junction points at the cold tier) and reports `pg_wal` separately;
  the effective budget is capped by the volume; the space ratchet is bounded per
  run, measures its 1 % drop on the WAL-free `tiering_usage_bytes`, and stops at
  `needs_repack` instead of running `VACUUM FULL`; every move statement uses
  explicit column lists (never `SELECT *`, never `ctid`), takes `FOR UPDATE` on
  the snapshot and inserts into the twin before deleting from hot; the
  incremental-chain clamp and its `chain_behind` / `chain_missing` /
  `chain_columns_missing` outcomes; deadline resolution including
  `deadline_missed`; the schema-drift comparison; the status → exit-code mapping;
  and the cutoff-index set of migration `20260919_0106` pinned against
  `TIER_POLICY`.
- `quant-service/tests/test_instrument_writer_lock_order.py` — walks every
  `.py` in the repository outside tests and vendored trees and requires each
  write to `quant.instruments` to live in `app/instrument_registry.py` or
  carry `ORDER BY 1` before its `ON CONFLICT` clause, to sit outside any
  loop, and to spell its table name literally. It reads the parsed source:
  the match ignores case, whitespace and identifier quoting, ends with its
  own literal, and an unparseable file is named as such. No allow-list: a new
  writer fails here until it takes the shared ascending lock order.
- `quant-service/tests/test_daily_bar_caller_lock_order.py` — the half the
  guard above cannot see: the four callers that drive
  `daily_bar_repository.upsert_daily_bar` once per bar must iterate
  `in_instrument_lock_order(bars)`, any fifth such loop under
  `quant-service/app` fails here (bare or module-qualified call), and
  `daily_bar_batch_repository`'s own `market_bars_daily` /
  `canonical_bars_daily` statements must send their keys ascending.
- `quant-service/tests/test_peer_batch_tunnel_deploy.py` — the peer-side batch
  port deploy: the committed fixture is a byte-for-byte copy of lightServer's
  live `ssh-tunnel-entrypoint.sh`, so a peer that changes fails the fixture hash
  and `KNOWN_PEER_STATES` together (intentional: both must be updated in one
  change); the patch result must pass `sh -n`, keep the peer's own `0.0.0.0`
  bind and be idempotent; and the already-deployed short-circuit must rest on
  the running container's own argv, never on an image tag a rebuild invalidates.
- `quant-service/tests/test_adjustment_factor_semantics_guard.py` — enforces
  "bar tables never receive a placeholder adjustment factor": no module may
  build a `factor_semantics: same_day_identity_only` row, only the pinned
  writers may `SET adj_factor` on a bar table, EVERY write site (the enclosing
  function of each match, not the file) must contain one of its module's
  pinned guards, and (with `PGHOST`) the release leak query is exercised
  against real PostgreSQL.

The same convention covers the Windows runtime. These PowerShell guard tests are
standalone (no database, no venv, no scheduled task) and run as
`pwsh -NoProfile -File <test>.ps1`; `publish-stock-release.ps1` executes them
before every release:

- `scripts/windows/tests/test-postgres-storage-tier-wiring.ps1` — the PowerShell
  runner and the Python CLI cannot drift: every `-Command` the runner offers is a
  real subcommand of `scripts/database-storage-tiers.py`; every flag the runner
  passes (including `--deadline` and `--max-seconds`) is a flag the CLI declares;
  the `status` → exit-code map stays 0 (`ok`/`deadline_reached`) / 1
  (`deadline_missed`, `partial`, `conflicts`, `schema_drift`) / 2 (`degraded`,
  `failed`) and the runner reports it verbatim — and that list is pinned back
  against the CLI's own `EXIT_CODES`, so a status added there without a line in
  the test turns the gate red rather than going unchecked; the run record stays at
  `logs\storage-tiers.jsonl`; the initializer seeds **no** dump exclusions and
  `backup-stock-database.ps1` computes them at dump time, so a cold twin is only
  left out when its hot table has an incremental chunk chain **whose watermark
  has kept up with the tier cutoff**; the two sides' hard-coded
  `STOCK_BACKUP_INCREMENTAL_TABLES` defaults (`Get-StockIncrementalTableSpecs` in
  PowerShell, `DEFAULT_INCREMENTAL_TABLES` in Python) are the same string, since
  the key is unset in production and both fall back to their own copy; the tier
  task registers **no** `-RestartCount`/`-RestartInterval` (a retry would triple
  the documented 7-days-per-table-per-run bound) while keeping
  `-StartWhenAvailable`; and `quant.storage_tier_conflicts` is never a `_cold`
  name, because the nightly dump must keep it.
- `scripts/windows/tests/test-postgres-io-window.ps1` — the pure window function
  `Get-PostgresIoWindowMode` across every boundary, plus the per-process loop
  (extracted from the script's own source, asserted detached from `Get-Process`
  and executed against fake processes with a stubbed `NtSetInformationProcess`):
  the I/O priority is re-issued on every run even when the priority class is
  unchanged, and a standing failure keeps being logged instead of vanishing after
  one line.
- `scripts/windows/tests/test-postgres-data-migration-contract.ps1` — the
  data-directory migration keeps its safety order (`Disable-ScheduledTask` →
  graceful `stop-stock-dashboard.ps1` → `Stop-ScheduledTask` backstop, the
  row-count snapshot taken **inside** the outage rather than in the preflight,
  verify the copy before switching `PGDATA_DIR`, rename the old directory instead
  of deleting it), recovers the platform from a failure in any step and rethrows
  — removing the partial copy it made at the target as the last recovery step,
  behind three guards and unlinking `pg_tblspc` junctions before the recursive
  delete — copies with `/XJ` while recreating the `pg_tblspc` junctions, and gates
  `-Rollback` behind `-AcceptDataLoss` plus two absolute refusals (no switch
  bypasses either): the image predates the `stock_cold` tablespace, or live and
  target resolve to the **same** tablespace location while the live checkpoint is
  later.
- `scripts/windows/tests/test-backup-stock-database.ps1` — the computed dump
  exclusion rule itself: chain present/absent, a watermark that is fresh, stale,
  exactly at the cutoff, missing or corrupt, a narrowed hot window flipping that
  same watermark, a failed incremental export, and an operator override naming a
  twin whose hot table has no chain (refused and reported, never silently
  honoured), plus `Get-StockIncrementalChainWatermark` against real `state.json`
  files on disk.
- `scripts/windows/tests/test-shared-tunnel-profiles.ps1` — the two tunnel
  profiles cannot collide or drift: distinct ports, forwarding tuples, service
  names, task names and lock files; the batch profile refuses to multiplex onto
  the intraday connection; and the install-time state-freshness judge refuses to
  certify a run whose own status says it is stopping or already over.
- `scripts/windows/tests/test-adjustment-factor-task-contract.ps1` — static
  contract for the 04:30 `trading-hareness-adjustment-factors` task: daily
  trigger, release-rooted hidden launcher, no restart-on-failure, cleared
  proxy variables, dated log file and the exit-code rule the schedule relies
  on. Registers no task and touches no database.

Two tests in this family are **not** in the release gate and must be run by hand:

- `scripts/windows/tests/test-stock-incremental-backup-drill.ps1` is not
  standalone and is not run by `publish-stock-release.ps1`: it starts the real
  PostgreSQL runtime and creates throw-away databases (dropped afterwards) to
  prove a nightly dump with exclusions can actually be restored, including the
  `no_chunk_chain` skip. Run it after changing the backup or restore path.
- `ScratchDatabaseMoveTest` in `quant-service/tests/test_database_storage_tiers.py`
  is skipped unless `STORAGE_TIERS_SCRATCH_TEST=1`, because it needs a reachable
  cluster and `CREATE DATABASE` rights that the gate does not have. It is the only
  test that **executes** `_move_table` — row conservation, quarantine, the chain
  clamp, a missing state file and a real `FOR UPDATE` lock against a scratch
  database it creates and drops itself. Run it after changing the move: the
  statement-shape assertions prove what the SQL looks like, not that it runs.

## Review automation

Analyst daily/weekly reviews are materialized by
`analyst_market_review.py`, persisted in `quant.analyst_market_reviews`, and
triggered by `strategy_review_scheduler.py`. The frontend reads them through
`/api/research/analyst-research/reviews/latest`; the regression is descriptive
and has `live_effect=none` until the documented sample gate is met.

For maintenance triage, read `/api/research/agent/context` first and then
`/api/research/automation/runs?task_key=...`. These are secret-free context and
durable execution evidence, not a substitute for source/test inspection.

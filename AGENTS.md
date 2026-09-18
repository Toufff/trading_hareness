# Repository agent guide

## Scope

This repository runs a market-research platform, not an order execution system.
All analyst scoring, regression, calibration, pattern mining and recommendations
are evidence-only until an explicit promotion record says otherwise. Never add a
provider response directly to a live threshold or order path.

## Change map

- `quant-service/app/routers/`: HTTP boundary and request validation.
- `quant-service/app/*_repository.py`: database read/write projections.
- `quant-service/app/instrument_registry.py`: the shared primitive for **bare
  symbol registration** in `quant.instruments` (symbol + exchange + source,
  `ON CONFLICT DO NOTHING`). A path that only needs the row to exist before it
  writes its own evidence must use `ensure_instruments` — one statement per
  5,000-symbol chunk, client-side deduplicated, sorted ascending so every
  writer takes the same lock order — and must not reintroduce a per-row
  `INSERT INTO quant.instruments ... DO NOTHING` loop. `ensure_instruments`
  returns the `(symbol, exchange)` pairs it actually wrote, and that return
  value is the contract: the helper strips and drops blanks, so a caller that
  writes a child row referencing `quant.instruments(symbol)` in the same
  transaction takes its symbols from the return value, never from its own raw
  input. It is **not** yet the
  only writer of that table, so do not assume instrument writes are globally
  centralised or globally ordered:
  - Converted to the helper: `tushare_normalization` (non-bar APIs),
    `public_market_repository.persist_market_events`,
    `sector_membership_repository.persist_ths_snapshot`,
    `intraday_minute_capture_actions`, `offline_minute_import_service`,
    `research_maintenance_service.update_universe_members`, and
    `remote_archive` (message and report signals).
  - Set-based and sorted but keeping their own SQL because they write more
    than the symbol (`ON CONFLICT DO UPDATE` — the strongest lock on this
    table: it locks every existing conflicting row, where `DO NOTHING` locks
    only newly inserted ones): `daily_bar_batch_repository` (sorted array),
    `tushare_normalization.persist_stock_basic_instruments` (one sorted
    `unnest` statement per `stock_basic` payload, `ORDER BY 1`), and every
    `INSERT INTO quant.instruments` in `annual_daily_backfill` — the two
    stage inserts and `_persist_stock_basic` — each with `ORDER BY 1`, pinned
    by `test_instrument_registry.SharedLockOrderAcrossWritersTests`.
  - **Not converted**, each still a per-row or attribute-carrying write:
    `broker_order_repository`,
    `broker_trade_repository`, `claim_review_service`, `daily_bar_repository`,
    `intraday_watchlist_service`, `main.py` (akshare, single symbol),
    `personal_decision_repository` (two sites), `strategy_decision_service`,
    and the injected
    per-row `ensure_instrument` that `main.persist_eastmoney_sector_members`
    hands to `sector_membership_repository.persist_observed_snapshot`. Most carry a
    name/industry that the `DO NOTHING` helper cannot express; several are
    genuinely single-symbol. They do not share the ascending lock order, so a
    deadlock between one of them and a batched writer is still possible until
    they are converted.
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

## Review automation

Analyst daily/weekly reviews are materialized by
`analyst_market_review.py`, persisted in `quant.analyst_market_reviews`, and
triggered by `strategy_review_scheduler.py`. The frontend reads them through
`/api/research/analyst-research/reviews/latest`; the regression is descriptive
and has `live_effect=none` until the documented sample gate is met.

For maintenance triage, read `/api/research/agent/context` first and then
`/api/research/automation/runs?task_key=...`. These are secret-free context and
durable execution evidence, not a substitute for source/test inspection.

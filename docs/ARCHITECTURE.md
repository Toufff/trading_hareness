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

`quant-service/app/main.py` is the composition root.  It owns application
lifespan, dependency assembly and router registration.  New behaviour belongs
in a focused module, then is injected from `main.py`; production modules must
not import `app.main`.

## Ownership boundaries

| Concern | Location | Rule |
|---|---|---|
| HTTP request validation | `app/routers/` | Router functions validate and delegate; no provider crawling in a read route. |
| Provider transport | `app/*provider*.py`, `app/http_clients.py` | Reuse lifecycle clients and record availability. |
| Persistent projections | `app/*_repository.py`, `app/*_read_model.py` | Bound result sets; async dashboard reads use `AsyncDatabase`. |
| Timing and recovery | `app/*_scheduler.py`, `app/runtime_tasks.py` | Durable leases, idempotent run keys and explicit retry windows. |
| Rules and research | `app/*_rules.py`, `app/*_research.py` | Keep inputs/outputs explicit and test without HTTP or database state. |
| Schema | `migrations/versions/` | New production schema changes use Alembic only. |
| Legacy bootstrap | `app/database.py` | Disabled by default; only an explicit recovery operator may enable it. |
| Frontend transport | `frontend/src/api/http.ts` | All JSON responses produce a readable non-JSON proxy error. |
| Frontend lifecycle | `frontend/src/composables/` | Timers and subscriptions are owned and stopped by the mounting shell. |
| Frontend feature UI | `frontend/src/components/`, `frontend/src/views/` | New dashboard surfaces must not grow `App.vue`. |

## Agent entry sequence

1. Read `GET /api/v1/agent/context` and the latest durable automation receipt.
2. Read the owning router, service, repository, migration and targeted test.
3. Preserve point-in-time boundaries: `stated_at` is replay evidence;
   `strategy_available_at` is the only strategy eligibility time.
4. Make one bounded change and add a focused test in the same domain.
5. Run backend tests, adapter tests, frontend API/type/build checks and
   `git diff --check`.
6. Verify mounted OpenAPI and `/health`; a source-only pass is not a runtime
   acceptance.

## Data and decision gates

- Missing/stale provider data, incomplete sector membership and insufficient
  statistical samples fail closed.
- The daily control plane applies to listed equities only.  Index benchmark
  rows are valid market context but do not carry equity `adj_factor` or
  `stk_limit` controls.
- `raw -> canonical -> features -> signals -> outcomes` is append/evidence
  oriented.  Replay outcomes never change live weights.
- Network loss keeps local loops alive.  Durable cursors, leases and run keys
  resume work after recovery without replaying completed work.

## Frontend ownership

`frontend/src/App.vue` is the shell only: navigation, lazy view registration
and shell-owned dialogs. `useDashboardWorkspace.ts` owns polling, SSE lifecycle
and mutations. Research tabs are independently lazy-loaded from
`frontend/src/views/research/`; relay monitoring and Feishu workbench are their
own views. New UI must join one of these owners instead of growing App.vue.

The frontend has unit coverage for the JSON transport and timer lifecycle, plus
a browser smoke test for the mounted research shell. API types remain generated
from the mounted OpenAPI contract.

The legacy DDL retained in `app/database.py` is recovery-only.  It remains
isolated behind `QUANT_LEGACY_SCHEMA_BOOTSTRAP`; new migrations must never edit
that bootstrap SQL.

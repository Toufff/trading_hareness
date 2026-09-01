# stock-brain migration into trading_hareness

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

The Windows bridge may read CITIC through MuMu but must never place, amend or
cancel an order.  The research service never controls the emulator directly.
It only accepts immutable, timestamped broker snapshots through a versioned
contract.

## Migration classes

### Import as durable facts

- settled daily and minute bars with provider and availability timestamps;
- sector membership, sector flow and stock order-size flow observations;
- broker position snapshots and journalled user trades;
- source documents, primary-evidence references and verified company facts;
- prediction, candidate and strategy outcomes with their original model
  versions and point-in-time boundaries.

The first implemented bridge imports the latest exact CITIC snapshot from
`stock-brain/daily/config.json`.  It requires one observation timestamp across
the account and every position, a CITIC read-only source marker, a real broker
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

### DecisionResearchDossier

The post-close scanner is not allowed to publish a research candidate directly
as a recommendation.  Each bounded candidate batch and every actual holding is
closed into a terminal dossier after the settled-close strategy stage:

- `passed`: all applicable checks are passed or explicitly advisory, including
  a separately executed downside case;
- `rejected`: at least one named check failed and the dossier explains the
  concrete reason in human language;
- `incomplete`: a required observation is genuinely unavailable.  Incomplete
  candidates never become new-buy plans and the public decision brief never
  tells the user that somebody should research them later.

The internal G0--G7 keys exist only for audit joins.  Human-facing output uses
their full labels: account/market eligibility, business identity, valuation
constraint, sector/catalyst, benefit mapping, price/liquidity trigger,
independent downside case and complete trade plan.  The downside check is a
separate function and does not consume the bullish score or scan rank.

A passed short-term dossier is a market-structure setup, not a claim that the
company is a long-term value investment.  Its `PersonalTradePlan` must still
include an entry range, invalidation, stop, position cap, target and validity
window.  Actual holdings receive a defensive plan from the exact broker
snapshot even when company research rejects the bullish case.

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

The real post-close orchestration now runs `decision_research_closure` after
`post_close_strategy` and `core_daily_controls`.  The stage is included in the
same durable receipt as the market refresh.  The personal decision surface is:

- `GET /api/v1/personal/portfolio-snapshots/latest?account_key=...`;
- `GET /api/v1/personal/decision-briefs/latest?account_key=...`;
- `GET /api/v1/personal/decision-research/latest`.

The decision brief and research audit are intentionally independent reads.  A
research-audit failure does not blank a valid market/holdings/new-buy brief, and
a broker failure only blocks the holdings section.  The frontend displays the
stock name first and its exchange code in parentheses on every first mention.

### Current migration acceptance snapshot

The 2026-09-01 real settled-close replay completed with no deferred stages.  It
persisted a usable but explicitly `degraded` market review (the current
multi-index/breadth evidence was incomplete), two exact holding dossiers,
twelve bounded candidate dossiers, one qualified conditional-buy plan and two
holding plans.  The current audit read returned 14 dossiers from the latest
model/current candidate batch only, with no unfinished dossier.  This is an
integration acceptance record, not an investment recommendation and not the
five-day shadow cutover required above.  A degraded market section remains
visible but can never make the overall decision brief claim `ready`.

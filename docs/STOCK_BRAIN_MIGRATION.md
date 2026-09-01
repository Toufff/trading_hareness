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

## Deployment boundary

- Linux/server: PostgreSQL, FastAPI, providers, strategy/research workers and
  Vue dashboard.
- Windows: CITIC read-only bridge and optional migration utilities.
- Transport: authenticated HTTP using versioned JSON; no shared SQLite file and
  no remote database access from the emulator process.

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

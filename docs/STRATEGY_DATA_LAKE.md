# Strategy evidence lake

The Baidu Netdisk personal application is a large immutable evidence tier, not
the live strategy database. PostgreSQL remains the hot point-in-time serving
store; local Parquet/DuckDB is the warm analytical cache; Netdisk is the durable
source, replay and experiment archive.

## Layered model

| Layer | Contents | Local policy | Cloud representation |
|---|---|---|---|
| L0 raw | provider payloads, analyst source reports/messages | short bounded cache | lossless `jsonl.zst` |
| L1 canonical | daily/minute bars, controls, order book, sector membership | frequently reused daily data stays hot; high-frequency data ages to warm | typed `parquet` with Zstandard |
| L2 features | versioned features, scan envelopes and frozen rule inputs | retain the current validation window | typed `parquet` with input/source hashes |
| L3 signals | candidates, rejected controls and state transitions | compact decision ledger stays hot | typed `parquet` by strategy/model |
| L4 outcomes | 5m/15m/30m/close/next-session, MFE/MAE and feasibility | stays hot for calibration | typed `parquet` by horizon/model |
| L5 reviews | experiments, ablations, analyst/strategy reviews | summaries stay hot | immutable run bundle plus metrics |
| L6 catalog | schemas, manifests, provenance, quality and restore receipts | local catalog | small JSON manifests |

The machine-readable placement contract lives in
`quant-service/app/platform/data_product_registry.py`. A dataset referenced by
a strategy or runtime task must be registered there before release validation
accepts it.

## Stable path layout

Use ASCII path segments below the existing application root so tooling does not
depend on UI-localized names:

```text
/apps/股票paper存储/quant-lake/v1/
  raw/dataset=<key>/provider=<provider>/available_date=YYYY-MM-DD/hour=HH/
  canonical/dataset=<key>/exchange_date=YYYY-MM-DD/symbol_bucket=NN/
  features/dataset=<key>/model_version=<version>/exchange_date=YYYY-MM-DD/
  strategies/strategy_key=<key>/model_version=<version>/run_date=YYYY-MM-DD/
  outcomes/strategy_key=<key>/model_version=<version>/horizon=<h>/exchange_date=YYYY-MM-DD/
  analyst/analyst_id=<id>/available_date=YYYY-MM-DD/
  manifests/dataset=<key>/schema_version=<version>/partition_id=<id>.json
```

Objects are immutable. A writer uploads to a unique staging prefix, verifies
bytes/hash, writes the final manifest last, then commits the manifest identity
to PostgreSQL. Existing objects are not accepted merely because their byte
length matches. Restore always targets a staging schema or warm cache and must
verify schema version, SHA-256, row count, min/max time and point-in-time
availability fields.

## Strategy improvements enabled by retained evidence

1. Intraday watchlist confirmation: retain every accepted and rejected
   snapshot, provider coverage, time-of-day state and 5m/15m/30m/close/next
   outcomes. This supplies unbiased controls for walk-forward timing
   challengers instead of learning only from fired alerts.
2. Ten-day leader rotation: retain exact as-known-at sector membership, all
   top-30 peers, minute VWAP/volume paths, one-word-board feasibility and next
   session MFE/MAE. This can test sector diffusion and leader lifecycle without
   hindsight mappings.
3. Xiaojie leader flow: preserve each qualitative source span beside the
   structured snapshot, mode and parameter hash. Evaluate modes separately by
   market regime and record every tried parameter set in the experiment ledger.
4. Post-close candidates and minute patterns: archive the full eligible
   universe and rejected controls, corporate-action inputs and minute paths.
   Use purged walk-forward splits and selection-bias-corrected metrics; never
   fit on the same dates used for reporting.
5. Analyst overlays: join only at `strategy_available_at`; compare market-only
   and bounded analyst-shadow legs. `stated_at` remains source/replay evidence
   and cannot make a claim available earlier.
6. Market regimes: retain breadth, board flows, previous-limit premium, index
   minute paths and style/sector transitions. Report every strategy outcome by
   regime instead of using one unconditional threshold across all sessions.

Large storage increases sample coverage, not decision authority. Every new
variant remains `live_effect=none`; promotion still requires independent days,
purged walk-forward evaluation, costs/feasibility, honest trial counts and a
separate promotion record.

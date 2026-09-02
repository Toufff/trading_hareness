# docs/ index

This is an index, not a reorganization: **no file listed here has moved.**
53 documents accumulated in this single flat directory across roughly a
month of daily research sessions, mixing executable runbooks, architecture
contracts and dated research notes with no separation, which the 2026-09
audit flagged as a real cost for anyone new joining the project. Moving the
files was rejected because several are linked by absolute path from other
docs, from `README.md`/`AGENTS.md`, and potentially from outside the
repository; this index instead groups them by kind so you can find the right
one without reading 53 titles.

If you are taking over this deployment, start at
[`AGENT_HANDOFF.md`](AGENT_HANDOFF.md), not here.

## Runbooks (executable operations)

Read these when you need to actually do something to the running system —
start it, hand it off, watch it, or recover it.

| Doc | What it's for |
|---|---|
| [`AGENT_HANDOFF.md`](AGENT_HANDOFF.md) | The authoritative entry point for taking over the running Windows platform: deployment paths, first-five-minutes health checks, publish/rollback commands, evidence locations. |
| [`SHARED_PEER_RUNTIME.md`](SHARED_PEER_RUNTIME.md) | Owner/lightServer/peer topology; bootstrap, tunnel setup, peer deployment, data migration and revocation — all executable steps. |
| [`OPENING_REALTIME_RUNBOOK.md`](OPENING_REALTIME_RUNBOOK.md) | Operating procedure for the market-open real-time alerting window. |

## Design (architecture and contracts)

Read these to understand how a subsystem is *supposed* to work — module
ownership, data contracts, algorithms and protocols that are not tied to a
single date. Several are large, some are explicitly `research-only` or
`experimental` in scope (noted below); none of that changes that they
describe a standing design, not a one-off finding.

| Doc | What it's for |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Platform-wide architecture, runtime map and ownership-boundary table. Read before any cross-domain change. |
| [`ARCHITECTURE_INDEX.md`](ARCHITECTURE_INDEX.md) | **Generated** — do not hand-edit. Produced by `scripts/generate_architecture_index.py`; regenerate it instead of editing it directly. |
| [`STOCK_BRAIN_MIGRATION.md`](STOCK_BRAIN_MIGRATION.md) | Old-system/new-system boundary, migration classes, `BrokerPortfolioSnapshot`/`PersonalTradePlan`/`PersonalDecisionBrief`/`DecisionResearchDossier` contracts, and cutover acceptance criteria. Carries a 2026-09-02 methodology-revision note. |
| [`STRATEGY_LOOP_V1.md`](STRATEGY_LOOP_V1.md) | Strategy loop v1: analyst text opinions, intraday confirmation and post-close review methodology. Carries a 2026-09-02 methodology-revision note. |
| [`STRATEGY_ANALYST_JOINT_SYSTEM_PLAN_V2.md`](STRATEGY_ANALYST_JOINT_SYSTEM_PLAN_V2.md) | Second architecture audit and implementation plan for the joint strategy/analyst system. |
| [`QUANT_RESEARCH_IMPLEMENTATION_PLAN.md`](QUANT_RESEARCH_IMPLEMENTATION_PLAN.md) | Analyst-channel-driven quant research platform implementation plan and phased acceptance criteria. |
| [`SHARED_STOCK_DATA_API.md`](SHARED_STOCK_DATA_API.md) | Complete read-only shared stock-data API contract used by the peer deployment. |
| [`STRATEGY_DATA_LAKE.md`](STRATEGY_DATA_LAKE.md) | Strategy evidence-lake contract. |
| [`STRATEGY_DECISION_MVP.md`](STRATEGY_DECISION_MVP.md) | `POST /api/v1/strategy/decisions/run` multi-source intraday/post-close decision contract. |
| [`DATA_TIERING_PROTOCOL.md`](DATA_TIERING_PROTOCOL.md) | Data tiering protocol (hot/warm/cold placement and replay role). |
| [`BAIDU_PAN_STORAGE.md`](BAIDU_PAN_STORAGE.md) | Baidu Pan personal-app storage adapter contract. |
| [`TUSHARE_COMPATIBLE_INGESTION.md`](TUSHARE_COMPATIBLE_INGESTION.md) | Tushare-compatible ingestion: interface scope, online limits, normalized tables, offline CSV format. |
| [`TUSHARE_OFFICIAL_CAPABILITY_MATRIX.md`](TUSHARE_OFFICIAL_CAPABILITY_MATRIX.md) | Tushare official-extension and dual-source capability matrix. |
| [`TUSHARE_PROVIDER_CAPABILITY_AUDIT.md`](TUSHARE_PROVIDER_CAPABILITY_AUDIT.md) | Tushare-compatible provider capability boundary — linked live from `DEPLOYMENT.md` as the current source of truth. |
| [`TUSHARE_SUPER_SDK_CAPABILITY_AUDIT.md`](TUSHARE_SUPER_SDK_CAPABILITY_AUDIT.md) | Super-source SDK/GET physical-channel capability design. |
| [`ANALYST_MESSAGE_POINT_IN_TIME.md`](ANALYST_MESSAGE_POINT_IN_TIME.md) | Point-in-time boundary definition for analyst messages. |
| [`ANALYST_SLEEPING_EXPERTS.md`](ANALYST_SLEEPING_EXPERTS.md) | Point-in-time settlement and Sleeping-Experts weighting design for analyst opinions. |
| [`ANALYST_TEXT_FACTORS.md`](ANALYST_TEXT_FACTORS.md) | Plain-text analyst factor archival and consensus design. |
| [`ANALYST_SKILL_DISTILLATION.md`](ANALYST_SKILL_DISTILLATION.md) | `research-only` methodology for distilling analyst language/skill signals from archived reports. |
| [`FACTOR_RESEARCH_AND_TRAINING.md`](FACTOR_RESEARCH_AND_TRAINING.md) | Factor research/backtest/training roadmap and Qlib/AlphaLens/LEAN/FinRL adapter boundaries. |
| [`INTRADAY_ALERTING.md`](INTRADAY_ALERTING.md) | Intraday anomaly-signal and alerting design contract. |
| [`INTRADAY_LIFECYCLE_AND_REPLAY_GOVERNANCE.md`](INTRADAY_LIFECYCLE_AND_REPLAY_GOVERNANCE.md) | Intraday signal lifecycle, replay and probability-governance protocol. |
| [`INTRADAY_GREEN_RECLAIM_RESEARCH.md`](INTRADAY_GREEN_RECLAIM_RESEARCH.md) | `experimental` intraday green-to-red-reclaim/sector-resonance pattern design; manual review only, no auto-order. |
| [`INTRADAY_UPSIDE_BREAKOUT_RESEARCH.md`](INTRADAY_UPSIDE_BREAKOUT_RESEARCH.md) | `experimental` first-breakout volume/flow-resonance pattern design; research `watch` events only. |
| [`REALTIME_STRATEGY_STRENGTHENING_PLAN.md`](REALTIME_STRATEGY_STRENGTHENING_PLAN.md) | Real-time strategy state-machine/evidence/probability strengthening design. |
| [`REALTIME_WATCHLIST_STRATEGY_RESEARCH_PLAN.md`](REALTIME_WATCHLIST_STRATEGY_RESEARCH_PLAN.md) | Watchlist real-time strategy strengthening design plan. |
| [`P1_ZERO_DOWNTIME_HANDOFF.md`](P1_ZERO_DOWNTIME_HANDOFF.md) | Zero-downtime service handoff boundary design (gateway sidecar, backend switch). |

## Research log (dated, read-only archive)

Each entry below is a point-in-time research finding, capability audit or
status snapshot. Treat the content as **what was observed/true on that
date**, not as a currently-maintained contract — a later doc or the live
system may have superseded it. Do not silently treat "the doc says done" as
proof of current state (the audit that produced this index found several
places where that assumption was wrong).

| Doc | Date / scope |
|---|---|
| [`RESEARCH_RECAP_2026-08-10.md`](RESEARCH_RECAP_2026-08-10.md) | 2026-08-10 full-day intraday research recap. |
| [`EXTERNAL_DATA_SOURCE_AND_RESEARCH_ROADMAP.md`](EXTERNAL_DATA_SOURCE_AND_RESEARCH_ROADMAP.md) | 2026-08-09 survey of external data sources by license/availability tier. |
| [`ANQIANG_AUTHOR_TIME_REPLAY_2026-08-10_TO_14.md`](ANQIANG_AUTHOR_TIME_REPLAY_2026-08-10_TO_14.md) | 2026-08-10–14 author-time replay of one analyst's calls. |
| [`ANQIANG_RECOMMENDED_BASKET_REVIEW_2026-08-10_TO_14.md`](ANQIANG_RECOMMENDED_BASKET_REVIEW_2026-08-10_TO_14.md) | 2026-08-10–14 weekly review of that analyst's recommended basket. |
| [`ANALYST_WEEKLY_BACKTEST_2026-08-10_TO_14.md`](ANALYST_WEEKLY_BACKTEST_2026-08-10_TO_14.md) | 2026-08-10–14 analyst weekly backtest and next-week scenarios. |
| [`MULTISCALE_VOLUME_FLOW_RESEARCH_2026-08-10_TO_14.md`](MULTISCALE_VOLUME_FLOW_RESEARCH_2026-08-10_TO_14.md) | 2026-08-10–14 multiscale volume/flow research. |
| [`SECTOR_FLOW_TRACE_AND_LHB_FACTOR_RESEARCH_2026-08-10_TO_14.md`](SECTOR_FLOW_TRACE_AND_LHB_FACTOR_RESEARCH_2026-08-10_TO_14.md) | 2026-08-10–14 sector-flow trace and Longhubang (dragon-tiger list) factor research. |
| [`POST_CLOSE_RECAP_2026-08-13.md`](POST_CLOSE_RECAP_2026-08-13.md) | 2026-08-13 post-close research recap. |
| [`WATCHLIST_MAIN_WAVE_RESEARCH_20260816.md`](WATCHLIST_MAIN_WAVE_RESEARCH_20260816.md) | 2026-08-16 watchlist main-wave-launch shadow model v1/v2. |
| [`WATCHLIST_REALTIME_STRATEGY_EVOLUTION_PLAN_20260816.md`](WATCHLIST_REALTIME_STRATEGY_EVOLUTION_PLAN_20260816.md) | 2026-08-16 watchlist real-time strategy evolution plan; `research_only`, historical bulk/minute replay paused pending authorization. |
| [`WATCHLIST_REALTIME_STRATEGY_RESEARCH_PROTOCOL_20260816.md`](WATCHLIST_REALTIME_STRATEGY_RESEARCH_PROTOCOL_20260816.md) | 2026-08-16 watchlist real-time strategy research/mining/promotion protocol. |
| [`COUNTERTREND_REBOUND_RESEARCH_20260816.md`](COUNTERTREND_REBOUND_RESEARCH_20260816.md) | 2026-08-16 tech-selloff counter-trend "B-wave" rebound shadow strategy. |
| [`FACTOR_EVALUATION_READINESS_20260816.md`](FACTOR_EVALUATION_READINESS_20260816.md) | 2026-08-16 factor-evaluation readiness and annualized live check. |
| [`AKSHARE_CAPABILITY_GAP_ANALYSIS.md`](AKSHARE_CAPABILITY_GAP_ANALYSIS.md) | 2026-08-20 AKShare capability-gap and complementary-ingestion analysis. |
| [`ANALYST_MARKET_EVALUATION_PLAN_20260821.md`](ANALYST_MARKET_EVALUATION_PLAN_20260821.md) | 2026-08-21 two-week analyst-opinion × market-flow evaluation/learning plan. |
| [`PROVIDER_INTERFACE_VERIFICATION_2026-08-21.md`](PROVIDER_INTERFACE_VERIFICATION_2026-08-21.md) | 2026-08-21 external data-interface acceptance record. |
| [`P0_DATA_CORRECTNESS_STATUS.md`](P0_DATA_CORRECTNESS_STATUS.md) | Updated 2026-08-22 — P0 data-correctness and backup status; records only what was actually verified, not what was planned. |
| [`MAINTENANCE_STATUS_20260822.md`](MAINTENANCE_STATUS_20260822.md) | 2026-08-22 platform maintenance-iteration status snapshot. |
| [`P1_EXIT_AUDIT.md`](P1_EXIT_AUDIT.md) | Updated 2026-08-24 — P1 hardening exit-audit handoff checklist. |
| [`DATAHUB_TUSHARE_CAPABILITY_AUDIT_20260826.md`](DATAHUB_TUSHARE_CAPABILITY_AUDIT_20260826.md) | 2026-08-26 DataHub Tushare-compatible source audit. |
| [`TUSHARE_SUPER_CAPABILITY_AUDIT.md`](TUSHARE_SUPER_CAPABILITY_AUDIT.md) | Explicitly marked outdated in its own title ("超级源旧路径模式审计（已过时）") — kept only as history; see `TUSHARE_SUPER_SDK_CAPABILITY_AUDIT.md` instead. |
| [`PLAN_COMPLETION_MATRIX.md`](PLAN_COMPLETION_MATRIX.md) | Status matrix for the joint quant/analyst system plan. **Historical snapshot** — see the banner at the top of the file; current state is `/api/v1/agent/context` and `strategy_registry`. |
| [`P1_RUNTIME_HARDENING_STATUS.md`](P1_RUNTIME_HARDENING_STATUS.md) | P1 runtime-hardening status log (101 KB). **Historical snapshot** — see the banner at the top of the file; current state is `/api/v1/agent/context` and `strategy_registry`. |

## Not indexed here

- [`legacy/MACOS_COLIMA_ERA.md`](legacy/MACOS_COLIMA_ERA.md) — archived macOS + Colima + Docker Compose runbook, superseded by the Windows platform. See `README.md`/`OPERATIONS.md` at the repository root.
- `screenshots/` — image assets referenced by some of the docs above, not documents themselves.

"""Application-level assembly for the bounded post-close refresh pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
import functools
from typing import Any

from .automation_run_repository import POST_CLOSE_STRATEGY_TASK_KEY, post_close_strategy_run_key, run_recorded
from .request_models import (
    AkShareProbeRequest,
    AnnouncementSyncRequest,
    FetchRunReconcileRequest,
    FullMarketDailySyncRequest,
    MarketSnapshotRequest,
    MarketUniverseSyncRequest,
    PostCloseStrategyRequest,
    SectorFlowSyncRequest,
    SnapshotRequest,
    StrategyDecisionRequest,
    StrategyPatternMiningRequest,
    WatchlistMainWaveResearchRequest,
)


POST_CLOSE_STAGE_ORDER = (
    "stale_fetch_runs", "analyst_text", "all_a_universe", "full_market_daily", "core_daily_controls", "index_context",
    "close_market_snapshot", "akshare_supplements", "ths_industry_flow", "ths_concept_flow_and_limit_strength",
    "market_flow_features", "limit_ladder", "limit_lift_pattern_mining", "cninfo_announcements",
    "board_review", "close_strategy_decision", "close_review", "analyst_outcomes", "analyst_intraday_outcomes",
    "analyst_scorecards", "analyst_expert_research", "post_close_strategy", "decision_research_closure",
    "watchlist_main_wave", "research_snapshot",
)

POST_CLOSE_TIMEOUT_OVERRIDES = {
    "akshare_supplements": 240.0,
    "limit_lift_pattern_mining": 120.0,
    # Four bounded full-market control APIs run sequentially so an individual
    # provider's shared limiter remains authoritative.
    "core_daily_controls": 240.0,
    # Outcome settlement scans retained evidence and is intentionally local.
    # Give both orchestration and the blocking repository the same bounded
    # window instead of inheriting the generic ten-second request budget.
    "analyst_outcomes": 300.0,
    "analyst_intraday_outcomes": 180.0,
}

POST_CLOSE_STAGE_DEPENDENCIES = {
    # Daily controls are correctness prerequisites: downstream strategy stages
    # must not reason over missing adjustment, limit or suspension fields.
    # Independent source evidence may still finish and remains diagnosable.
    "index_context": ("core_daily_controls",),
    "limit_ladder": ("core_daily_controls",),
    "limit_lift_pattern_mining": ("core_daily_controls", "limit_ladder"),
    "close_strategy_decision": ("core_daily_controls",),
    "close_review": ("core_daily_controls",),
    "post_close_strategy": ("core_daily_controls",),
    "watchlist_main_wave": ("core_daily_controls",),
    "research_snapshot": ("core_daily_controls",),
    "decision_research_closure": ("post_close_strategy", "core_daily_controls"),
}


@dataclass(frozen=True)
class PostCloseRefreshDependencies:
    """Local actions required by one post-close refresh; no provider is owned here."""

    database: Any
    china_today: Callable[[], date]
    longhu_configured: Callable[[], bool]
    longhu_close_context: Callable[[date], dict[str, Any]]
    provider_configs: Callable[[], dict[str, Any]]
    run_database: Callable[..., Awaitable[Any]]
    reconcile_stale_fetch_runs: Callable[[Any], Any]
    reprocess_remote_reports: Callable[..., Any]
    sync_market_universe: Callable[[Any], Awaitable[dict[str, Any]]]
    sync_full_market_daily: Callable[[Any], Awaitable[dict[str, Any]]]
    sync_strategy_index_context: Callable[[date], Awaitable[dict[str, Any]]]
    build_market_snapshot: Callable[[Any], Awaitable[dict[str, Any]]]
    load_core_symbols: Callable[[int], Awaitable[list[str]]]
    akshare_probe: Callable[[Any], Awaitable[dict[str, Any]]]
    sync_ths_industry_flow: Callable[[Any], Awaitable[dict[str, Any]]]
    sync_ths_concept_flow: Callable[[Any], Awaitable[dict[str, Any]]]
    rebuild_market_flow_features: Callable[..., Any]
    refresh_pattern_sources: Callable[[date], Awaitable[dict[str, Any]]]
    persist_settled_limit_pool: Callable[[Any, date], dict[str, Any]]
    run_pattern_mining: Callable[[Any], Awaitable[dict[str, Any]]]
    sync_daily_controls: Callable[[date], Awaitable[dict[str, Any]]]
    sync_cninfo_announcements: Callable[[Any], Awaitable[dict[str, Any]]]
    run_board_report: Callable[..., Awaitable[dict[str, Any]]]
    run_strategy_decision: Callable[[Any], Awaitable[dict[str, Any]]]
    persist_close_review: Callable[[date], Any]
    recompute_outcomes: Callable[[date], Any]
    recompute_intraday_outcomes: Callable[[date], Any]
    recompute_scorecards: Callable[[date], Any]
    rebuild_analyst_research: Callable[[date], Any]
    run_post_close_strategy: Callable[[Any], Any]
    refresh_decision_research: Callable[[Any, date], dict[str, Any]]
    persist_watchlist_main_wave: Callable[[Any], Any]
    build_research_snapshot: Callable[[Any], Any]
    run_orchestrator: Callable[..., Awaitable[dict[str, Any]]]
    record_stage: Callable[..., Awaitable[Any]]
    lease_key: str
    lease_seconds: Callable[[], int]
    acquire_lease: Callable[..., Any]
    renew_lease: Callable[..., Any]
    release_lease: Callable[..., Any]
    safe_error_detail: Callable[[str, int], str]
    json_safe: Callable[[Any], Any]
    check_lease_fence: Callable[[Any, str, int], Awaitable[Any]] | None = None
    # Reaper (audit B-HIGH): a killed process leaves automation_runs rows
    # stuck 'running' forever. Reconciled at the same stage/entry point as
    # the existing fetch_runs reconcile rather than adding a new one.
    reconcile_stale_automation_runs: Callable[[], Any] | None = None
    # Must match the scheduled loop's methodology_version cosmetically only
    # (run_key is the actual dedup key); kept as its own field so this module
    # does not need to import main.py's model-version constant directly.
    post_close_strategy_model_version: str = "post-close-strategy-v1"


async def run_post_close_refresh(request: Any, dependencies: PostCloseRefreshDependencies) -> dict[str, Any]:
    """Assemble the same bounded post-close stages and durable receipts.

    The function creates no provider client and contains no historical replay
    path.  It only preserves the existing same-date refresh workflow.
    """
    trade_date = request.trade_date or dependencies.china_today()
    longhu_mode = dependencies.longhu_configured()
    super_get = dependencies.provider_configs().get("super_get")
    full_market_daily_provider = (
        "super_get"
        if super_get and super_get.configured and super_get.get_gateway_mode == "promax" and super_get.supports("daily")
        else "auto"
    )
    core_symbols: list[str] = []

    async def akshare_stage() -> dict[str, Any]:
        nonlocal core_symbols
        core_symbols = await dependencies.load_core_symbols(request.announcement_limit)
        if longhu_mode:
            return {
                "status": "skipped",
                "reason": "optional AkShare probe is outside the Longhu authoritative close path",
                "core_symbols": len(core_symbols),
            }
        probe_symbol = core_symbols[0] if core_symbols else "000636.SZ"
        return await dependencies.akshare_probe(AkShareProbeRequest(
            symbol=probe_symbol, trade_date=trade_date,
            include_macro_cross_asset=request.include_macro_cross_asset, board_limit=30,
        ))

    async def announcements_stage() -> dict[str, Any]:
        if not request.include_announcements or not core_symbols:
            return {"status": "skipped", "reason": "disabled or core universe is empty"}
        return await dependencies.sync_cninfo_announcements(AnnouncementSyncRequest(
            symbols=core_symbols, universe_key="core", start_date=trade_date - timedelta(days=45),
            end_date=trade_date, max_pages_per_symbol=1,
        ))

    async def limit_ladder_stage() -> dict[str, Any]:
        if longhu_mode:
            return await dependencies.run_database(
                dependencies.persist_settled_limit_pool, dependencies.database, trade_date,
                timeout_seconds=60,
            )
        return await dependencies.refresh_pattern_sources(trade_date)

    async def stale_fetch_runs_stage() -> dict[str, Any]:
        result = await dependencies.run_database(
            dependencies.reconcile_stale_fetch_runs, FetchRunReconcileRequest(max_age_minutes=90),
        )
        # Reconcile orphaned automation_runs rows (a killed process's
        # 'running' receipt) at this same maintenance entry point rather
        # than adding a separate scheduled task.
        if dependencies.reconcile_stale_automation_runs is not None:
            automation_result = await dependencies.run_database(dependencies.reconcile_stale_automation_runs)
            if isinstance(result, dict):
                result = {**result, "stale_automation_runs": automation_result}
        return result

    actions: dict[str, Callable[[], Any]] = {
        "stale_fetch_runs": stale_fetch_runs_stage,
        "analyst_text": lambda: dependencies.run_database(dependencies.reprocess_remote_reports, dependencies.database, 500),
        "all_a_universe": lambda: dependencies.sync_market_universe(MarketUniverseSyncRequest()),
        "full_market_daily": lambda: dependencies.sync_full_market_daily(
            FullMarketDailySyncRequest(trade_date=trade_date, provider=full_market_daily_provider),
        ),
        "index_context": lambda: dependencies.sync_strategy_index_context(trade_date),
        "close_market_snapshot": lambda: dependencies.build_market_snapshot(
            MarketSnapshotRequest(session="close", universe_key="all_a", refresh_public_quotes=False),
        ),
        "akshare_supplements": akshare_stage,
        "ths_industry_flow": (
            lambda: dependencies.run_database(dependencies.longhu_close_context, trade_date)
            if longhu_mode else
            dependencies.sync_ths_industry_flow(SectorFlowSyncRequest(trade_date=trade_date, provider="super"))
        ),
        "ths_concept_flow_and_limit_strength": (
            lambda: {
                "status": "skipped",
                "reason": "Longhu close supplies exact THS industry membership and flow, not concept flow",
                "provider": "longhuvip_composite",
            }
            if longhu_mode else
            dependencies.sync_ths_concept_flow(SectorFlowSyncRequest(trade_date=trade_date, provider="super"))
        ),
        "market_flow_features": lambda: dependencies.run_database(
            dependencies.rebuild_market_flow_features, dependencies.database, trade_date, trade_date, timeout_seconds=90,
        ),
        "limit_ladder": limit_ladder_stage,
        "limit_lift_pattern_mining": lambda: dependencies.run_pattern_mining(
            StrategyPatternMiningRequest(as_of_date=trade_date, refresh_limit_sources=False),
        ),
        "core_daily_controls": lambda: dependencies.sync_daily_controls(trade_date),
        "cninfo_announcements": announcements_stage,
        "board_review": (
            lambda: dependencies.run_database(dependencies.longhu_close_context, trade_date)
            if longhu_mode else dependencies.run_board_report(deliver=False)
        ),
        "close_strategy_decision": (
            lambda: {
                "status": "skipped",
                "reason": "legacy provider-coupled close decision is superseded by the persisted post-close strategy stage",
                "replacement_stage": "post_close_strategy",
            }
            if longhu_mode else
            dependencies.run_strategy_decision(
                StrategyDecisionRequest(session="close", kind="all", limit=20, validate_tushare_realtime=False),
            )
        ),
        "close_review": lambda: dependencies.run_database(dependencies.persist_close_review, trade_date),
        "analyst_outcomes": lambda: dependencies.run_database(
            dependencies.recompute_outcomes, trade_date, timeout_seconds=300,
        ),
        "analyst_intraday_outcomes": lambda: dependencies.run_database(
            dependencies.recompute_intraday_outcomes, trade_date, timeout_seconds=180,
        ),
        "analyst_scorecards": lambda: dependencies.run_database(dependencies.recompute_scorecards, trade_date),
        "analyst_expert_research": lambda: dependencies.run_database(dependencies.rebuild_analyst_research, trade_date),
        # Shares its task_key/run_key with the scheduled post-close loop
        # (strategy_runtime_runners.run_post_close_strategy_loop) via
        # automation_run_repository.post_close_strategy_run_key: previously
        # this manual stage only got the generic per-stage receipt
        # (POST_CLOSE_RECEIPT_VERSION:post_close_strategy:<date>) which the
        # scheduler never checked, so the 18:55-20:30 overlap window let
        # both triggers write the same strategy tables concurrently.
        "post_close_strategy": lambda: dependencies.run_database(functools.partial(
            run_recorded, dependencies.database, task_key=POST_CLOSE_STRATEGY_TASK_KEY,
            run_key=post_close_strategy_run_key(trade_date),
            operation=functools.partial(
                dependencies.run_post_close_strategy, PostCloseStrategyRequest(as_of_date=trade_date),
            ),
            cadence="daily", as_of_date=trade_date,
            methodology_version=dependencies.post_close_strategy_model_version,
            input_summary={"data_boundary": "same_date_close", "trigger": "manual_refresh"},
        )),
        "decision_research_closure": lambda: dependencies.run_database(
            dependencies.refresh_decision_research, dependencies.database, trade_date, timeout_seconds=120,
        ),
        "watchlist_main_wave": lambda: dependencies.run_database(
            dependencies.persist_watchlist_main_wave, WatchlistMainWaveResearchRequest(as_of_date=trade_date),
        ),
        "research_snapshot": lambda: dependencies.run_database(
            dependencies.build_research_snapshot, SnapshotRequest(as_of_date=trade_date),
        ),
    }

    async def record_refresh_stage(name: str, stage_date: date, action: Callable[[], Any]) -> Any:
        return await dependencies.record_stage(
            name, stage_date, action, db=dependencies.database, run_database_blocking=dependencies.run_database,
            safe_error_detail=dependencies.safe_error_detail,
        )

    return await dependencies.run_orchestrator(
        request, db=dependencies.database, lease_key=dependencies.lease_key,
        lease_seconds=dependencies.lease_seconds, run_database_blocking=dependencies.run_database,
        acquire_lease=dependencies.acquire_lease, renew_lease=dependencies.renew_lease,
        release_lease=dependencies.release_lease, actions=actions, stage_order=POST_CLOSE_STAGE_ORDER,
        timeout_overrides=POST_CLOSE_TIMEOUT_OVERRIDES, stage_dependencies=POST_CLOSE_STAGE_DEPENDENCIES,
        record_stage=record_refresh_stage, trade_date=trade_date,
        safe_error_detail=dependencies.safe_error_detail, json_safe=dependencies.json_safe,
        check_lease_fence=dependencies.check_lease_fence,
    )


__all__ = [
    "POST_CLOSE_STAGE_DEPENDENCIES", "POST_CLOSE_STAGE_ORDER", "POST_CLOSE_TIMEOUT_OVERRIDES",
    "PostCloseRefreshDependencies", "run_post_close_refresh",
]

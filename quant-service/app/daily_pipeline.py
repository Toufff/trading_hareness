"""Dependency-injected daily market pipeline orchestration."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable


async def run_pipeline(
    payload: Any,
    *,
    sync_tushare: Callable[[Any], Awaitable[dict[str, Any]]],
    sync_baostock: Callable[[Any], Awaitable[dict[str, Any]]],
    sync_tushare_daily_core: Callable[[date | None], Awaitable[dict[str, Any]]],
    tushare_request: Any,
    snapshot_request: Callable[[date | None], Any],
    build_snapshot: Callable[..., Any],
    recompute_outcomes: Callable[..., Any],
    recompute_scorecards: Callable[..., Any],
    generate_recommendations: Callable[..., Any],
    run_database_blocking: Callable[..., Awaitable[Any]],
    cn_today: Callable[[], date],
    sync_earnings_calendar: Callable[[date], Awaitable[dict[str, Any]]] | None = None,
    materialize_regime: Callable[[date], Any] | None = None,
    materialize_candidate_ledger: Callable[[date], Any] | None = None,
    materialize_watchlist_proposals: Callable[[date], Any] | None = None,
) -> dict[str, Any]:
    primary = await sync_tushare(tushare_request(trade_date=payload.as_of_date))
    fallback = None
    if primary["status"] in {"disabled", "partial", "failed"}:
        fallback = await sync_baostock(tushare_request(trade_date=payload.as_of_date))
    core = await sync_tushare_daily_core(payload.as_of_date or cn_today()) if primary["status"] in {"completed", "unchanged", "partial"} else None
    sync = {"primary": primary, "fallback": fallback, "core": core}
    snapshot = await run_database_blocking(build_snapshot, snapshot_request(payload.as_of_date), timeout_seconds=30)
    if snapshot["status"] != "ready":
        return {"status": "blocked", "market_sync": sync, "snapshot": snapshot,
                "reason": "行情数据或质量门禁未满足；没有生成候选池"}
    as_of_date = payload.as_of_date or cn_today()
    # The reporting calendar is fetched before proposals so the disclosure-day
    # watch source sees today's registered schedule for the next session.  Its
    # own failure is reported, never fatal: the price-based ledger below does
    # not depend on it.
    earnings_calendar = (await sync_earnings_calendar(as_of_date)
                         if sync_earnings_calendar is not None else None)
    regime = (await run_database_blocking(materialize_regime, as_of_date, timeout_seconds=30)
             if materialize_regime is not None else None)
    # Ledger materialization reads whatever each strategy's own table already
    # holds for as_of_date; it does not require those strategies to run here.
    ledger = (await run_database_blocking(materialize_candidate_ledger, as_of_date, timeout_seconds=30)
             if materialize_candidate_ledger is not None else None)
    # Proposals are read after the ledger materializes so they see today's
    # candidates; this never writes into intraday_watchlists (see
    # watchlist_candidate_proposals.py for why).
    watchlist_proposals = (await run_database_blocking(materialize_watchlist_proposals, as_of_date, timeout_seconds=30)
                           if materialize_watchlist_proposals is not None else None)
    outcomes = await run_database_blocking(recompute_outcomes, payload.as_of_date, timeout_seconds=60)
    scorecard = await run_database_blocking(recompute_scorecards, payload.as_of_date, timeout_seconds=30)
    result = await run_database_blocking(generate_recommendations, payload, timeout_seconds=30)
    return {"status": "completed", "market_sync": sync, "snapshot": snapshot, "regime": regime,
            "earnings_calendar": earnings_calendar, "candidate_ledger": ledger,
            "watchlist_proposals": watchlist_proposals, "outcomes": outcomes,
            "scorecards": scorecard, "recommendations": result}


__all__ = ["run_pipeline"]

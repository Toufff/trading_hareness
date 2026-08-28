"""Dependency-injected daily market pipeline orchestration."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Awaitable, Callable

#: A bounded ceiling on the minute-bar pass, which is the one stage that talks
#: to a slow, per-symbol upstream. The rest of the pipeline runs in well under
#: this; the cap only stops a stalling route from holding the whole post-close
#: run open. Whatever it does not reach is filled in by the next day's re-run.
MINUTE_BACKFILL_BUDGET_SECONDS = 600


async def run_pipeline(
    payload: Any,
    *,
    sync_full_market_daily: Callable[[Any], Awaitable[dict[str, Any]]],
    sync_baostock: Callable[[Any], Awaitable[dict[str, Any]]],
    sync_full_market_daily_controls: Callable[[date], Awaitable[dict[str, Any]]],
    tushare_request: Any,
    full_market_request: Any,
    snapshot_request: Callable[[date | None], Any],
    build_snapshot: Callable[..., Any],
    recompute_outcomes: Callable[..., Any],
    recompute_scorecards: Callable[..., Any],
    generate_recommendations: Callable[..., Any],
    run_database_blocking: Callable[..., Awaitable[Any]],
    cn_today: Callable[[], date],
    sync_earnings_calendar: Callable[[date], Awaitable[dict[str, Any]]] | None = None,
    sync_stock_money_flow: Callable[[date], Awaitable[dict[str, Any]]] | None = None,
    materialize_regime: Callable[[date], Any] | None = None,
    materialize_sentiment_cycle: Callable[[date], Any] | None = None,
    materialize_candidate_ledger: Callable[[date], Any] | None = None,
    materialize_watchlist_proposals: Callable[[date], Any] | None = None,
    settle_xiaojie_outcomes: Callable[[date], Any] | None = None,
    backfill_minute_bars: Callable[[date], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    # Both market stages take the whole cross-section in one request. The
    # per-symbol synchronizers this replaced issued one call per name over the
    # full ~5.5k universe: measured on 2026-08-27 they imported about 1.8 bars
    # a minute, so a session's bars would have taken some 46 hours and the
    # HTTP request died at 851s long before settlement ran. The same date
    # loaded in 20s here.
    as_of = payload.as_of_date or cn_today()
    primary = await sync_full_market_daily(full_market_request(trade_date=payload.as_of_date))
    fallback = None
    if primary["status"] in {"blocked", "disabled", "partial", "failed"}:
        fallback = await sync_baostock(tushare_request(trade_date=payload.as_of_date))
    # Controls carry the limit prices every downstream limit-up judgement reads,
    # and they refuse to run against a partially fetched date on their own.
    controls = (await sync_full_market_daily_controls(as_of)
                if primary["status"] in {"completed", "unchanged", "partial"} else None)
    sync = {"primary": primary, "fallback": fallback, "controls": controls}
    snapshot = await run_database_blocking(build_snapshot, snapshot_request(payload.as_of_date), timeout_seconds=30)
    if snapshot["status"] != "ready":
        return {"status": "blocked", "market_sync": sync, "snapshot": snapshot,
                "reason": "行情数据或质量门禁未满足；没有生成候选池"}
    as_of_date = as_of
    # The reporting calendar is fetched before proposals so the disclosure-day
    # watch source sees today's registered schedule for the next session.  Its
    # own failure is reported, never fatal: the price-based ledger below does
    # not depend on it.
    earnings_calendar = (await sync_earnings_calendar(as_of_date)
                         if sync_earnings_calendar is not None else None)
    # Per-stock flow is end-of-day only and never feeds a live rule; it is
    # ingested here so post-close and backtest work can finally ask whether
    # main flow preceded anything.  Its failure is reported, never fatal.
    stock_money_flow = (await sync_stock_money_flow(as_of_date)
                        if sync_stock_money_flow is not None else None)
    regime = (await run_database_blocking(materialize_regime, as_of_date, timeout_seconds=30)
             if materialize_regime is not None else None)
    # The index regime and the board tape are different readings of the same
    # session, and the study behind this one showed the second separates
    # outcomes the first does not.  Both are stored before anything downstream
    # stratifies by them.
    sentiment_cycle = (await run_database_blocking(materialize_sentiment_cycle, as_of_date,
                                                   timeout_seconds=60)
                       if materialize_sentiment_cycle is not None else None)
    # Ledger materialization reads whatever each strategy's own table already
    # holds for as_of_date; it does not require those strategies to run here.
    ledger = (await run_database_blocking(materialize_candidate_ledger, as_of_date, timeout_seconds=30)
             if materialize_candidate_ledger is not None else None)
    # Proposals are read after the ledger materializes so they see today's
    # candidates; this never writes into intraday_watchlists (see
    # watchlist_candidate_proposals.py for why).
    watchlist_proposals = (await run_database_blocking(materialize_watchlist_proposals, as_of_date, timeout_seconds=30)
                           if materialize_watchlist_proposals is not None else None)
    # Settling turns accumulated leader-flow observations into an evaluable
    # record.  Re-running refreshes: the next session's bars do not exist yet
    # at this point, so the forward columns fill in on the following day's run.
    xiaojie_outcomes = (await run_database_blocking(settle_xiaojie_outcomes, as_of_date, timeout_seconds=60)
                        if settle_xiaojie_outcomes is not None else None)
    outcomes = await run_database_blocking(recompute_outcomes, payload.as_of_date, timeout_seconds=60)
    scorecard = await run_database_blocking(recompute_scorecards, payload.as_of_date, timeout_seconds=30)
    # Measured at 36-38s over the ~5.5k core universe on 2026-08-27; the 30s
    # budget it used to carry cancelled the stage every run, which is why the
    # pipeline reported an internal error at the very last step.
    result = await run_database_blocking(generate_recommendations, payload, timeout_seconds=180)
    # Minute bars for the session's boards and benchmarks, gathered last. The
    # route is slow and only partly available - stk_mins answered ~55% of
    # sampled boards over three closed sessions and 0% intraday - so it runs
    # after every decision output already exists, and its failure is reported,
    # never fatal. The budget stops a stalling upstream from holding the run
    # open; whatever it misses a re-run backfills, since the write is
    # idempotent and settles against the same session.
    minute_bars = None
    if backfill_minute_bars is not None:
        try:
            minute_bars = await asyncio.wait_for(
                backfill_minute_bars(as_of_date), timeout=MINUTE_BACKFILL_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            minute_bars = {"status": "timeout", "budget_seconds": MINUTE_BACKFILL_BUDGET_SECONDS}
        except Exception as error:  # noqa: BLE001 - a research backfill never fails the pipeline
            minute_bars = {"status": "failed", "error": f"{type(error).__name__}: {str(error)[:200]}"}
    return {"status": "completed", "market_sync": sync, "snapshot": snapshot, "regime": regime,
            "minute_bars": minute_bars,
            "sentiment_cycle": sentiment_cycle,
            "earnings_calendar": earnings_calendar, "stock_money_flow": stock_money_flow,
            "candidate_ledger": ledger, "xiaojie_outcomes": xiaojie_outcomes,
            "watchlist_proposals": watchlist_proposals, "outcomes": outcomes,
            "scorecards": scorecard, "recommendations": result}


__all__ = ["run_pipeline"]

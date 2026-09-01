"""Session-scoped minute-bar backfill over events, matched controls and benchmarks.

Minute bars are the missing input behind several blocked questions: what time
of day a board sealed and whether that predicted the next session, how a
candidate behaved against VWAP, and - the one that bit on 2026-08-27 - a
time-matched market benchmark, which had to be replaced by a cross-sectional
median because ``000001.SH`` had no minute bars that day.

The primitives this composes are already proven: ``normalize_minute_rows``
collapses the upstream's double delivery, and ``reconcile_against_daily``
checked one session's minute volume against its daily bar to 0.000%.

**Not wired into the daily pipeline, deliberately.** ``stk_mins`` answered 1
of 18 sampled symbols on 2026-08-26 - the rest returned HTTP 202 with a
``Retry-After`` that never resolved. Scheduling a nightly pass over a route
answering 6% of the time would burn the request budget to little effect.
``coverage_report`` exists so that availability is measured before anyone
schedules this, rather than discovered weeks later in an empty table.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .minute_bar_backfill import backfill_symbol_session

#: Benchmarks a time-matched comparison needs; without these a strategy's
#: intraday return can only be judged against a cross-sectional median.
BENCHMARK_SYMBOLS: tuple[str, ...] = ("000001.SH", "000300.SH", "000905.SH", "000852.SH")
#: A session's board count runs 50-100 names; the cap bounds a pathological day
#: rather than trimming an ordinary one, and truncation is reported.
MAX_SESSION_SYMBOLS = 200
MAX_NEAR_LIMIT_SYMBOLS = 40
MAX_TREND_SYMBOLS = 40
MAX_MATCHED_CONTROL_SYMBOLS = 40


def session_symbols(connection: Any, trading_date: date, *,
                    benchmarks: Sequence[str] = BENCHMARK_SYMBOLS,
                    limit: int = MAX_SESSION_SYMBOLS) -> dict[str, Any]:
    """Select event names and negative controls from persisted daily evidence.

    A replay set made only from sealed boards cannot distinguish a limit-up
    pattern from a broadly strong tape.  In addition to all locked limits that
    fit the cap, retain bounded near-limit names and liquidity/return-matched
    non-limit controls.  The query uses the day's canonical limit price rather
    than a fixed 10% assumption, so ChiNext, STAR and ST names keep their own
    market rules.  Benchmarks are never dropped by the cap.
    """
    event_limit = max(0, min(int(limit), MAX_SESSION_SYMBOLS))
    near_limit = min(MAX_NEAR_LIMIT_SYMBOLS, max(0, event_limit))
    trend_limit = min(MAX_TREND_SYMBOLS, max(0, event_limit))
    control_limit = min(MAX_MATCHED_CONTROL_SYMBOLS, max(0, event_limit))
    rows = connection.execute(
        """WITH history AS (
               SELECT symbol,trading_date,close,pre_close,volume,limit_up,
                      avg(close) OVER (PARTITION BY symbol ORDER BY trading_date
                                      ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                      lag(close,20) OVER (PARTITION BY symbol ORDER BY trading_date) AS close_20d
                 FROM quant.canonical_bars_daily
                WHERE trading_date<=%s AND trading_date>=%s::date-interval '45 days'
                  AND volume>0 AND NOT coalesce(is_suspended,false)
             ), daily AS (
               SELECT symbol,close,pre_close,volume,limit_up,ma20,close_20d
                 FROM history WHERE trading_date=%s
             ), board_all AS (
               SELECT symbol,close,pre_close,volume,limit_up FROM daily
                WHERE limit_up IS NOT NULL AND close>=limit_up-0.005
             ), boards AS (
               SELECT symbol FROM board_all ORDER BY symbol LIMIT %s
             ), near_limit AS (
               SELECT d.symbol FROM daily d
               WHERE d.limit_up IS NOT NULL AND d.close>=d.limit_up*0.97 AND d.close<d.limit_up-0.005
                  AND NOT EXISTS (SELECT 1 FROM board_all board WHERE board.symbol=d.symbol)
                ORDER BY (d.limit_up-d.close)/nullif(d.limit_up,0),d.symbol LIMIT %s
             ), trend_all AS (
               SELECT d.symbol FROM daily d
                WHERE d.ma20 IS NOT NULL AND d.close_20d IS NOT NULL
                  AND d.close>d.ma20 AND d.close/d.close_20d-1>=0.08
                  AND NOT EXISTS (SELECT 1 FROM board_all board WHERE board.symbol=d.symbol)
                  AND NOT EXISTS (SELECT 1 FROM near_limit near WHERE near.symbol=d.symbol)
                ORDER BY d.close/d.close_20d DESC,d.symbol LIMIT %s
             ), controls AS (
               SELECT DISTINCT ON (board.symbol) candidate.symbol
                 FROM board_all board
                 CROSS JOIN LATERAL (
                   SELECT d.symbol,d.close,d.pre_close,d.volume
                     FROM daily d
                    WHERE right(d.symbol,2)=right(board.symbol,2)
                      AND d.symbol<>board.symbol
                      AND d.close<coalesce(d.limit_up,d.close*1.10)*0.94
                      AND NOT EXISTS (SELECT 1 FROM board_all locked WHERE locked.symbol=d.symbol)
                      AND NOT EXISTS (SELECT 1 FROM near_limit near WHERE near.symbol=d.symbol)
                      AND NOT EXISTS (SELECT 1 FROM trend_all trend WHERE trend.symbol=d.symbol)
                    ORDER BY abs(coalesce(d.close/nullif(d.pre_close,0),0)-coalesce(board.close/nullif(board.pre_close,0),0)),
                             abs(coalesce(d.volume/nullif(board.volume,0),0)-1),d.symbol
                    LIMIT 1
                 ) candidate
                ORDER BY board.symbol,candidate.symbol
             ), capped_controls AS (
               SELECT symbol FROM controls ORDER BY symbol LIMIT %s
             )
             SELECT 'board' AS sample_role,symbol,(SELECT count(*)::int FROM board_all) AS source_total FROM boards
             UNION ALL SELECT 'near_limit',symbol,(SELECT count(*)::int FROM near_limit) FROM near_limit
             UNION ALL SELECT 'trend',symbol,(SELECT count(*)::int FROM trend_all) FROM trend_all
             UNION ALL SELECT 'matched_control',symbol,(SELECT count(*)::int FROM controls) FROM capped_controls
             ORDER BY sample_role,symbol""",
        (trading_date, trading_date, trading_date, event_limit, near_limit, trend_limit, control_limit),
    ).fetchall()
    selections: dict[str, list[str]] = {"board": [], "near_limit": [], "trend": [], "matched_control": []}
    totals: dict[str, int] = {}
    for row in rows:
        item = dict(row)
        role = str(item.get("sample_role") or "board")
        if role not in selections:
            continue
        selections[role].append(str(item["symbol"]))
        if item.get("source_total") is not None:
            totals[role] = int(item["source_total"])
    ordered = list(dict.fromkeys([
        *benchmarks, *selections["board"], *selections["near_limit"],
        *selections["trend"], *selections["matched_control"],
    ]))
    total_boards = totals.get("board", len(selections["board"]))
    return {
        "symbols": ordered, "boards": total_boards,
        "near_limit": len(selections["near_limit"]), "trend": len(selections["trend"]),
        "matched_controls": len(selections["matched_control"]),
        "truncated": max(0, total_boards - len(selections["board"])),
        "benchmarks": list(benchmarks), "sample_roles": selections,
    }


async def backfill_session(
    trading_date: date, *,
    symbols: Sequence[str],
    selection_roles: Mapping[str, Sequence[str]] | None = None,
    call_tushare_api: Callable[..., Awaitable[Any]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
) -> dict[str, Any]:
    """Fetch each symbol's minute bars for one session, one request at a time.

    Sequential on purpose: the upstream serves one ts_code per request and the
    gateway is budgeted per minute, so a fan-out here would spend the budget
    faster without finishing sooner.  A symbol that fails is recorded and the
    pass continues - one unavailable name must not cost the session.
    """
    results: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            results.append(await backfill_symbol_session(
                symbol, trading_date, call_tushare_api=call_tushare_api,
                run_database_blocking=run_database_blocking, db=db,
                selection_roles=(selection_roles or {}).get(symbol, ())))
        except Exception as error:  # noqa: BLE001 - one symbol must not end the pass
            results.append({"symbol": symbol, "trading_date": str(trading_date),
                            "status": "failed", "bars": 0,
                            "error": f"{type(error).__name__}: {str(error)[:200]}"})
    return {"trading_date": str(trading_date), "requested": len(symbols),
            "results": results, **coverage_report(results)}


def coverage_report(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summarise what the upstream actually answered.

    Availability is reported as its own number because it is the fact that
    decides whether scheduling this is worth anything: at the 6% measured on
    2026-08-26 a nightly pass is budget spent for almost nothing.
    """
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    answered = counts.get("completed", 0) + counts.get("partial", 0)
    total = len(results)
    return {"status_counts": counts, "answered": answered,
            "availability_pct": round(100.0 * answered / total, 2) if total else None,
            "bars": sum(int(result.get("bars") or 0) for result in results)}


__all__ = [
    "BENCHMARK_SYMBOLS", "MAX_MATCHED_CONTROL_SYMBOLS", "MAX_NEAR_LIMIT_SYMBOLS", "MAX_SESSION_SYMBOLS", "MAX_TREND_SYMBOLS", "backfill_session", "coverage_report",
    "session_symbols",
]

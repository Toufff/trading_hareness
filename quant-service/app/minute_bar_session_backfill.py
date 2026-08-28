"""Session-scoped minute-bar backfill over the day's boards and the benchmarks.

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
from typing import Any, Awaitable, Callable, Sequence

from .minute_bar_backfill import backfill_symbol_session, limit_up_symbols

#: Benchmarks a time-matched comparison needs; without these a strategy's
#: intraday return can only be judged against a cross-sectional median.
BENCHMARK_SYMBOLS: tuple[str, ...] = ("000001.SH", "000300.SH", "000905.SH", "000852.SH")
#: A session's board count runs 50-100 names; the cap bounds a pathological day
#: rather than trimming an ordinary one, and truncation is reported.
MAX_SESSION_SYMBOLS = 200


def session_symbols(connection: Any, trading_date: date, *,
                    benchmarks: Sequence[str] = BENCHMARK_SYMBOLS,
                    limit: int = MAX_SESSION_SYMBOLS) -> dict[str, Any]:
    """The names worth minute bars for one session: its boards, plus benchmarks.

    Benchmarks are never dropped by the cap - they are the reason a session is
    comparable at all, and they are a handful of symbols.
    """
    boards = limit_up_symbols(connection, trading_date, trading_date).get(trading_date, [])
    kept = boards[:max(0, limit)]
    ordered = list(dict.fromkeys([*benchmarks, *kept]))
    return {"symbols": ordered, "boards": len(boards),
            "truncated": max(0, len(boards) - len(kept)),
            "benchmarks": list(benchmarks)}


async def backfill_session(
    trading_date: date, *,
    symbols: Sequence[str],
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
                run_database_blocking=run_database_blocking, db=db))
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
    "BENCHMARK_SYMBOLS", "MAX_SESSION_SYMBOLS", "backfill_session", "coverage_report",
    "session_symbols",
]

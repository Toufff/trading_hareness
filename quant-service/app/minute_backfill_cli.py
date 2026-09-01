"""Drive the minute-bar session backfill over a date range.

The library pieces already exist -- ``session_symbols`` picks each session's
limit-up pool, trend/near-threshold cohorts, matched controls and benchmarks,
and ``backfill_session`` walks them one request at a time because the upstream
serves a single ts_code per call. What was missing is a way to run them over a
range and resume, which is what this adds.

Bounded on purpose: the whole point of scoping to the limit-up pool is that a
full-market minute backfill would not fit the gateway budget. Progress is
printed per session so a long run can be watched, and a session already covered
is skipped so the command can be re-run.

    python -m app.minute_backfill_cli --start-date 2026-06-01 --end-date 2026-08-31
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime

from .main import call_tushare_api, db, run_database_blocking
from .minute_bar_session_backfill import backfill_session, coverage_report, session_symbols


def _iso(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


# run_database_blocking hands the action no connection, so each one opens its own
# short transaction through the shared pool.
def _open_days(start: date, end: date) -> list[date]:
    with db.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT trading_date FROM quant.canonical_bars_daily "
                "WHERE trading_date BETWEEN %s AND %s ORDER BY trading_date",
                (start, end),
            )
            # The pool is configured with a dict row factory.
            return [row["trading_date"] for row in cursor.fetchall()]


def _covered_symbols(trading_date: date, symbols: list[str]) -> set[str]:
    if not symbols:
        return set()
    with db.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT symbol FROM quant.market_bars_minute
                   WHERE symbol=ANY(%s)
                     AND (bar_time AT TIME ZONE 'Asia/Shanghai')::date=%s""",
                (symbols, trading_date),
            )
            return {str(row["symbol"]) for row in cursor.fetchall()}


def _session_symbols(trading_date: date, limit: int) -> dict:
    with db.transaction() as connection:
        return session_symbols(connection, trading_date, limit=limit)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True, type=_iso)
    parser.add_argument("--end-date", required=True, type=_iso)
    parser.add_argument("--limit", type=int, default=60,
                        help="max limit-up names per session; benchmarks are always kept")
    parser.add_argument("--min-covered", type=int, default=0,
                        help="legacy compatibility; 0 skips only when every selected symbol is covered")
    args = parser.parse_args()

    days = await run_database_blocking(_open_days, args.start_date, args.end_date)
    print(json.dumps({"status": "started", "open_days": len(days),
                      "start": str(args.start_date), "end": str(args.end_date)}), flush=True)

    done = skipped = failed = 0
    for index, trading_date in enumerate(days, start=1):
        picked = await run_database_blocking(_session_symbols, trading_date, args.limit)
        symbols = picked["symbols"]
        if not symbols:
            skipped += 1
            continue
        covered_symbols = await run_database_blocking(_covered_symbols, trading_date, symbols)
        # A prior run may contain only the old board/benchmark cohort.  Do not
        # call that day complete until every currently selected trend, near-limit
        # and matched-control symbol also has a stored minute session.
        if len(covered_symbols) == len(set(symbols)):
            skipped += 1
            continue
        symbols = [symbol for symbol in symbols if symbol not in covered_symbols]
        selection_roles: dict[str, list[str]] = {symbol: ["benchmark"] for symbol in picked.get("benchmarks", [])}
        for role, role_symbols in dict(picked.get("sample_roles", {})).items():
            for symbol in role_symbols:
                selection_roles.setdefault(str(symbol), []).append(str(role))
        try:
            outcome = await backfill_session(
                trading_date, symbols=symbols, call_tushare_api=call_tushare_api,
                run_database_blocking=run_database_blocking, db=db, selection_roles=selection_roles)
            report = coverage_report(outcome.get("results", []))
            done += 1
            print(json.dumps({"day": str(trading_date), "progress": f"{index}/{len(days)}",
                              "symbols": len(symbols), "coverage": report,
                              "skipped": skipped, "failed": failed}, default=str), flush=True)
        except Exception as error:  # noqa: BLE001 - one session must not end the run
            failed += 1
            print(json.dumps({"day": str(trading_date), "progress": f"{index}/{len(days)}",
                              "error": str(error)[:200]}), flush=True)

    print(json.dumps({"status": "finished", "sessions_done": done,
                      "skipped": skipped, "failed": failed}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

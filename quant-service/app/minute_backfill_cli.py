"""Drive the minute-bar session backfill over a date range.

The library pieces already exist -- ``session_symbols`` picks each session's
limit-up pool plus the benchmarks, and ``backfill_session`` walks them one
request at a time because the upstream serves a single ts_code per call. What
was missing is a way to run them over a range and resume, which is what this
adds.

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


def _already_covered(trading_date: date) -> int:
    with db.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(DISTINCT symbol) FROM quant.market_bars_minute "
                "WHERE bar_time >= %s::date AND bar_time < (%s::date + 1) ",
                (trading_date, trading_date),
            )
            row = cursor.fetchone()
            return int((row or {}).get("count") or 0)


def _session_symbols(trading_date: date, limit: int) -> dict:
    with db.transaction() as connection:
        return session_symbols(connection, trading_date, limit=limit)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True, type=_iso)
    parser.add_argument("--end-date", required=True, type=_iso)
    parser.add_argument("--limit", type=int, default=60,
                        help="max limit-up names per session; benchmarks are always kept")
    parser.add_argument("--min-covered", type=int, default=5,
                        help="skip a session that already has at least this many symbols")
    args = parser.parse_args()

    days = await run_database_blocking(_open_days, args.start_date, args.end_date)
    print(json.dumps({"status": "started", "open_days": len(days),
                      "start": str(args.start_date), "end": str(args.end_date)}), flush=True)

    done = skipped = failed = 0
    for index, trading_date in enumerate(days, start=1):
        covered = await run_database_blocking(_already_covered, trading_date)
        if covered >= args.min_covered:
            skipped += 1
            continue
        picked = await run_database_blocking(_session_symbols, trading_date, args.limit)
        symbols = picked["symbols"]
        if not symbols:
            skipped += 1
            continue
        try:
            outcome = await backfill_session(
                trading_date, symbols=symbols, call_tushare_api=call_tushare_api,
                run_database_blocking=run_database_blocking, db=db)
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

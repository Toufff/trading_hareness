"""Drive the minute-bar session backfill over a date range.

This module contains the command logic but does not import the ASGI composition
root. The executable wrapper under ``scripts/`` supplies database, provider and
executor dependencies explicitly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any, Awaitable, Callable, Sequence

from .minute_bar_session_backfill import backfill_session, coverage_report, session_symbols


@dataclass(frozen=True)
class MinuteBackfillCliDependencies:
    database: Any
    call_tushare_api: Callable[..., Awaitable[Any]]
    run_database_blocking: Callable[..., Awaitable[Any]]


def _iso(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True, type=_iso)
    parser.add_argument("--end-date", required=True, type=_iso)
    parser.add_argument(
        "--limit", type=int, default=60,
        help="max limit-up names per session; benchmarks are always kept",
    )
    parser.add_argument(
        "--min-covered", type=int, default=0,
        help="legacy compatibility; 0 skips only when every selected symbol is covered",
    )
    return parser.parse_args(argv)


def _open_days(database: Any, start: date, end: date) -> list[date]:
    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT trading_date FROM quant.canonical_bars_daily "
                "WHERE trading_date BETWEEN %s AND %s ORDER BY trading_date",
                (start, end),
            )
            return [row["trading_date"] for row in cursor.fetchall()]


def _covered_symbols(database: Any, trading_date: date, symbols: list[str]) -> set[str]:
    if not symbols:
        return set()
    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT symbol FROM quant.market_bars_minute
                   WHERE symbol=ANY(%s)
                     AND (bar_time AT TIME ZONE 'Asia/Shanghai')::date=%s""",
                (symbols, trading_date),
            )
            return {str(row["symbol"]) for row in cursor.fetchall()}


def _session_symbols(database: Any, trading_date: date, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        return session_symbols(connection, trading_date, limit=limit)


async def run(args: argparse.Namespace, deps: MinuteBackfillCliDependencies) -> int:
    days = await deps.run_database_blocking(_open_days, deps.database, args.start_date, args.end_date)
    print(json.dumps({
        "status": "started", "open_days": len(days),
        "start": str(args.start_date), "end": str(args.end_date),
    }), flush=True)

    done = skipped = failed = 0
    for index, trading_date in enumerate(days, start=1):
        picked = await deps.run_database_blocking(_session_symbols, deps.database, trading_date, args.limit)
        symbols = picked["symbols"]
        if not symbols:
            skipped += 1
            continue
        covered_symbols = await deps.run_database_blocking(
            _covered_symbols, deps.database, trading_date, symbols,
        )
        if len(covered_symbols) == len(set(symbols)):
            skipped += 1
            continue
        symbols = [symbol for symbol in symbols if symbol not in covered_symbols]
        selection_roles: dict[str, list[str]] = {
            symbol: ["benchmark"] for symbol in picked.get("benchmarks", [])
        }
        for role, role_symbols in dict(picked.get("sample_roles", {})).items():
            for symbol in role_symbols:
                selection_roles.setdefault(str(symbol), []).append(str(role))
        try:
            outcome = await backfill_session(
                trading_date,
                symbols=symbols,
                call_tushare_api=deps.call_tushare_api,
                run_database_blocking=deps.run_database_blocking,
                db=deps.database,
                selection_roles=selection_roles,
            )
            report = coverage_report(outcome.get("results", []))
            done += 1
            print(json.dumps({
                "day": str(trading_date), "progress": f"{index}/{len(days)}",
                "symbols": len(symbols), "coverage": report,
                "skipped": skipped, "failed": failed,
            }, default=str), flush=True)
        except Exception as error:  # noqa: BLE001 - one session must not end the range
            failed += 1
            print(json.dumps({
                "day": str(trading_date), "progress": f"{index}/{len(days)}",
                "error": str(error)[:200],
            }), flush=True)

    print(json.dumps({
        "status": "finished", "sessions_done": done,
        "skipped": skipped, "failed": failed,
    }), flush=True)
    return 0


async def main(deps: MinuteBackfillCliDependencies, argv: Sequence[str] | None = None) -> int:
    return await run(parse_args(argv), deps)


__all__ = ["MinuteBackfillCliDependencies", "main", "parse_args", "run"]


if __name__ == "__main__":
    raise SystemExit(
        "Use scripts/run-minute-backfill.py so runtime dependencies are assembled outside app modules."
    )

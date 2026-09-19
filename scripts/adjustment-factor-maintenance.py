#!/usr/bin/env python3
"""Cumulative daily adjustment factors from longhu -- lane, repair, validation.

This is the executable wrapper for ``app.adjustment_factor_maintenance``.
Every factor is derived from the licensed longhu daily kline
(``app.longhu_adjustment_factors``); nothing here calls tushare.

Subcommands:

``sync``      the nightly lane (04:30 task and the post-close stage's CLI
              twin): derive the factors of every pending settled date.
              ``--dry-run`` lists the work without any provider call or write.
``status``    read-only report of where the lane stands.
``repair``    the idempotent one-time backfill of the damaged window.  WITHOUT
              ``--apply`` it runs on a READ-ONLY connection
              (``default_transaction_read_only=on``) and prints the plan: dates,
              actions found by each kind of evidence, disagreements with stored
              factors, anchors missing, the projected release guard and every
              bar that would stay NULL with its reason.  ``--apply`` writes one
              transaction per trading date and reads everything back.
``validate``  read-only: re-derive a stored tushare period and score the method.

stdout is ASCII-only JSON: the scheduled-task host console is GBK.  No
credential value is ever printed; the env file is loaded into ``os.environ``
and nothing reads it back out.

Exit codes: 0 ok (including coverage-skipped dates and a dry run); 1 a date
failed, the repair left the release guard above 0, or the fetch failed.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

DEFAULT_ENV_FILE = r"G:\StockPlatform\config\runtime.env"
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)
    sync_command = subcommands.add_parser(
        "sync", help="derive cumulative factors for every settled date still missing them")
    sync_command.add_argument("--lookback-days", type=int, default=30)
    sync_command.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    sync_command.add_argument(
        "--dry-run", action="store_true",
        help="resolve and print the pending dates without any provider call or write")
    status_command = subcommands.add_parser(
        "status",
        help="print where the factor lane stands; reads only, fetches nothing, writes nothing")
    status_command.add_argument("--lookback-days", type=int, default=30)
    status_command.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    repair_command = subcommands.add_parser(
        "repair", help="one-time backfill of the damaged window (dry run unless --apply)")
    repair_command.add_argument("--from", dest="from_date", type=date.fromisoformat, default=None,
                                help="first date (default: derived from the data)")
    repair_command.add_argument("--to", dest="to_date", type=date.fromisoformat, default=None,
                                help="last date (default: the latest settled date)")
    repair_command.add_argument("--lookback-sessions", type=int, default=60,
                                help="how far back a damaged date is searched for")
    repair_command.add_argument("--apply", action="store_true",
                                help="write; without it the run uses a read-only connection")
    repair_command.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    validate_command = subcommands.add_parser(
        "validate", help="read-only: score the derivation against a stored tushare period")
    validate_command.add_argument("--from", dest="from_date", type=date.fromisoformat,
                                  default=date(2026, 6, 1))
    validate_command.add_argument("--to", dest="to_date", type=date.fromisoformat,
                                  default=date(2026, 8, 26))
    validate_command.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    return parser.parse_args(argv)


def load_env_file(path: str) -> int:
    """Load KEY=VALUE lines into the process environment; never print a value."""
    loaded = 0
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()
        loaded += 1
    return loaded


def clear_proxies() -> None:
    """A desktop proxy turns a working local route into a nightly false failure."""
    for name in PROXY_VARIABLES:
        os.environ.pop(name, None)


class ReadOnlyDatabase:
    """``transaction()`` on a connection the SERVER refuses to write through.

    Used by ``repair`` without ``--apply`` and by ``validate``: the dry run is
    safe against production by construction, not by care.
    """

    def __init__(self) -> None:
        from psycopg.rows import dict_row

        from app.db_dsn import connection_params

        self._kwargs = {**connection_params(), "row_factory": dict_row, "connect_timeout": 10,
                        "options": "-c default_transaction_read_only=on -c statement_timeout=600000"}

    @contextmanager
    def transaction(self):
        import psycopg

        with psycopg.connect(**self._kwargs) as connection:
            with connection.transaction():
                yield connection


async def _to_thread(action, *args, timeout_seconds: float | None = None, **kwargs):
    return await asyncio.to_thread(functools.partial(action, *args, **kwargs))


def read_only_dependencies():
    from app.adjustment_factor_maintenance import AdjustmentFactorMaintenanceDependencies
    from app.longhu_vendor_source import intraday_source

    return AdjustmentFactorMaintenanceDependencies(
        database=ReadOnlyDatabase(), run_database=_to_thread, longhu_source=intraday_source,
        run_public=_to_thread, safe_error_detail=lambda value, limit: str(value)[:limit])


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    load_env_file(args.env_file)
    clear_proxies()

    from app.adjustment_factor_maintenance import FAILED_STATUS  # noqa: E402

    if args.command == "validate":
        from app.adjustment_factor_maintenance import validate

        report = asyncio.run(validate(read_only_dependencies(), from_date=args.from_date,
                                      to_date=args.to_date))
        print(json.dumps(report, ensure_ascii=True, default=str))
        return 0

    if args.command == "repair" and not args.apply:
        from app.adjustment_factor_maintenance import repair

        report = asyncio.run(repair(read_only_dependencies(), apply=False, from_date=args.from_date,
                                    to_date=args.to_date, lookback_sessions=args.lookback_sessions))
        print(json.dumps(report, ensure_ascii=True, default=str))
        return 1 if report.get("status") == FAILED_STATUS else 0

    # Imported after the env file is loaded: the composition root builds its
    # connection pool at import time from these variables.  The dependency set
    # itself lives in app.main so the scheduled task, the post-close stage and
    # this CLI cannot drift into three different compositions.
    from app.main import (  # noqa: E402
        adjustment_factor_status,
        repair_adjustment_factors,
        sync_adjustment_factors,
    )

    if args.command == "status":
        # Read-only: no provider call, no write, no exit code of its own.
        report = asyncio.run(adjustment_factor_status(args.lookback_days))
        print(json.dumps(report, ensure_ascii=True, default=str))
        return 0

    if args.command == "repair":
        report = asyncio.run(repair_adjustment_factors(
            apply=True, from_date=args.from_date, to_date=args.to_date,
            lookback_sessions=args.lookback_sessions))
        print(json.dumps(report, ensure_ascii=True, default=str))
        return 1 if report.get("status") == FAILED_STATUS else 0

    result = asyncio.run(sync_adjustment_factors(args.lookback_days, dry_run=args.dry_run))
    # ASCII-only on purpose: a reason can carry Chinese text and the task host
    # decodes this stdout under GBK.
    print(json.dumps(result, ensure_ascii=True, default=str))
    # Only a longhu failure or an exception is this job's failure.  A date whose
    # own daily cross-section is too thin is reported as skipped with its
    # reason and exits 0, because the factor lane cannot repair it and a
    # nightly non-zero exit for it would alert forever.
    return 1 if result.get("status") == FAILED_STATUS else 0


if __name__ == "__main__":
    raise SystemExit(main())

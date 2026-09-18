#!/usr/bin/env python3
"""Fill missing cumulative daily adjustment factors outside the post-close path.

This is the executable wrapper for ``app.adjustment_factor_maintenance``.  It
runs in the 04:00-08:00 maintenance window and for the one-time repair
documented in ``docs/ADJUSTMENT_FACTOR_SEMANTICS.md``; it is deliberately not
a post-close stage, because the adjustment-factor provider must never be able
to delay or fail the evening close pipeline.

stdout is ASCII-only JSON: the scheduled-task host console is GBK.  No
credential value is ever printed; the env file is loaded into ``os.environ``
and nothing reads it back out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

DEFAULT_ENV_FILE = r"G:\StockPlatform\config\runtime.env"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    sync_command = subcommands.add_parser(
        "sync", help="fetch cumulative factors for every settled date still missing them")
    sync_command.add_argument("--lookback-days", type=int, default=30)
    sync_command.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    sync_command.add_argument(
        "--dry-run", action="store_true",
        help="resolve and print the pending dates without any provider call or write")
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


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    load_env_file(args.env_file)

    # Imported after the env file is loaded: the composition root builds its
    # connection pool at import time from these variables.
    from app.adjustment_factor_maintenance import (  # noqa: E402
        AdjustmentFactorMaintenanceDependencies, sync,
    )
    from app.main import (  # noqa: E402
        ExecutorSaturatedError, call_tushare_api, db, persist_tushare_fetch_blocked,
        persist_tushare_rows, record_provider_api_capability, record_provider_failure,
        record_provider_success, run_database_blocking, safe_error_detail, tushare_date,
    )

    dependencies = AdjustmentFactorMaintenanceDependencies(
        database=db,
        run_database=run_database_blocking,
        call_tushare_api=call_tushare_api,
        parse_tushare_date=tushare_date,
        persist_tushare_rows=persist_tushare_rows,
        persist_blocked=persist_tushare_fetch_blocked,
        safe_error_detail=safe_error_detail,
        executor_saturated_error=ExecutorSaturatedError,
        record_provider_success=record_provider_success,
        record_provider_failure=record_provider_failure,
        record_provider_api_capability=record_provider_api_capability,
    )
    result = asyncio.run(sync(
        dependencies, lookback_days=args.lookback_days, dry_run=args.dry_run,
    ))
    # ASCII-only on purpose: a blocked reason can carry Chinese text and the
    # task host decodes this stdout under GBK.
    print(json.dumps(result, ensure_ascii=True, default=str))
    return 0 if result.get("status") in {"completed", "planned", "unchanged"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

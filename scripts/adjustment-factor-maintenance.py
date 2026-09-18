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
    # connection pool at import time from these variables.  The dependency set
    # itself lives in app.main so the scheduled task, the post-close stage and
    # this CLI cannot drift into three different compositions.
    from app.adjustment_factor_maintenance import FAILED_STATUS  # noqa: E402
    from app.main import sync_adjustment_factors  # noqa: E402

    result = asyncio.run(sync_adjustment_factors(args.lookback_days, dry_run=args.dry_run))
    # ASCII-only on purpose: a blocked reason can carry Chinese text and the
    # task host decodes this stdout under GBK.
    print(json.dumps(result, ensure_ascii=True, default=str))
    # Only a provider error or an exception is this job's failure.  A date the
    # daily-controls coverage gate refuses is reported as skipped with its
    # reason and exits 0, because the factor lane cannot repair a thin daily
    # cross-section and a nightly non-zero exit for it would alert forever.
    return 1 if result.get("status") == FAILED_STATUS else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Composition wrapper for the bounded minute-session backfill."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

from app.main import call_tushare_api, db, run_database_blocking  # noqa: E402
from app.minute_backfill_cli import MinuteBackfillCliDependencies, main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(MinuteBackfillCliDependencies(
        database=db,
        call_tushare_api=call_tushare_api,
        run_database_blocking=run_database_blocking,
    ))))

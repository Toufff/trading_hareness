#!/usr/bin/env python3
"""Snapshot and migrate durable stock-brain SQLite data into PostgreSQL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

from app.legacy_stock_brain_archive import (  # noqa: E402
    LegacyStockBrainArchiveImporter,
    create_consistent_snapshot,
    sha256_file,
)
from app.legacy_stock_brain_repository import LegacyStockBrainRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(r"F:\AIWorkflow\stock-brain\db\brain.db"))
    parser.add_argument(
        "--archive-root", type=Path,
        default=Path(r"G:\StockPlatform\data\imports\stock-brain"),
    )
    parser.add_argument("--snapshot", type=Path, help="reuse an existing immutable SQLite snapshot")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    if args.snapshot:
        snapshot = args.snapshot.resolve(strict=True)
        digest = sha256_file(snapshot)
    else:
        snapshot, digest = create_consistent_snapshot(args.source, args.archive_root)

    repository = LegacyStockBrainRepository()
    try:
        importer = LegacyStockBrainArchiveImporter(
            repository,
            batch_size=args.batch_size,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
        result = importer.run(snapshot, digest)
    finally:
        repository.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

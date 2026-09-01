#!/usr/bin/env python3
"""Project a captured Fuyao daily dump into a typed, research-only parquet.

The provider dump is immutable source evidence.  This command deliberately
does not write PostgreSQL canonical tables: its availability clock is the
capture/import time, not the original vendor publication time.  The output is
registered in the local warm catalog and can then be uploaded by the existing
``upload_parquet_to_pan.py`` workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, "marketdata")
CATALOG = os.path.join(ROOT, "catalog", "catalog.duckdb")
DATASET = "fuyao_daily_10y"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project(source: str, output: str, *, available_at: str | None = None) -> dict[str, object]:
    parquet = pq.ParquetFile(source)
    expected = {"thscode", "date_ms", "open_price", "high_price", "low_price", "close_price", "volume", "turnover"}
    missing = sorted(expected - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"Fuyao dump missing required columns: {', '.join(missing)}")
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    writer: pq.ParquetWriter | None = None
    rows = 0
    min_date = max_date = None
    try:
        for batch in parquet.iter_batches(batch_size=100_000):
            frame = batch.to_pandas()
            frame = frame.rename(columns={
                "thscode": "symbol", "open_price": "open", "high_price": "high",
                "low_price": "low", "close_price": "close", "turnover": "amount",
            })
            frame["trading_date"] = pd.to_datetime(frame.pop("date_ms"), unit="ms", utc=True).dt.date
            frame["source"] = "fuyao_ths_bulk"
            frame["availability_basis"] = "provider_received_at_import_v1"
            frame["available_at"] = available_at or datetime.now(timezone.utc).isoformat()
            frame["research_only"] = True
            frame = frame[[
                "symbol", "trading_date", "open", "high", "low", "close", "volume", "amount",
                "interval", "adjusted", "source", "availability_basis", "available_at", "research_only",
            ]]
            table = pa.Table.from_pandas(frame, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(output, table.schema, compression="zstd")
            writer.write_table(table)
            rows += len(frame)
            batch_min, batch_max = frame["trading_date"].min(), frame["trading_date"].max()
            min_date = batch_min if min_date is None or batch_min < min_date else min_date
            max_date = batch_max if max_date is None or batch_max > max_date else max_date
    finally:
        if writer is not None:
            writer.close()
    if rows == 0:
        raise ValueError("Fuyao dump contains no rows")
    return {"rows": rows, "date_min": str(min_date), "date_max": str(max_date), "bytes": os.path.getsize(output), "sha256": sha256_file(output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--output", default=os.path.join(ROOT, "parquet", DATASET, "2016-2026.parquet"))
    parser.add_argument("--available-at", help="UTC ISO timestamp from the capture manifest")
    args = parser.parse_args()
    receipt = project(args.source, args.output, available_at=args.available_at)
    con = duckdb.connect(CATALOG)
    con.execute(
        """INSERT INTO partitions(dataset,symbol,partition_key,local_path,rows,bytes,sha256,date_min,date_max,source,adjust,ingested_at)
           VALUES (?, '__ALL__', '2016-2026', ?, ?, ?, ?, ?, ?, 'fuyao_ths_bulk', 'none', now())
           ON CONFLICT(dataset,symbol,partition_key) DO UPDATE SET local_path=EXCLUDED.local_path,rows=EXCLUDED.rows,
             bytes=EXCLUDED.bytes,sha256=EXCLUDED.sha256,date_min=EXCLUDED.date_min,date_max=EXCLUDED.date_max,
             source=EXCLUDED.source,adjust=EXCLUDED.adjust,ingested_at=now(),pan_path=NULL,pan_fs_id=NULL,uploaded_at=NULL""",
        [DATASET, args.output, receipt["rows"], receipt["bytes"], receipt["sha256"], receipt["date_min"], receipt["date_max"]],
    )
    con.close()
    print(json.dumps({"dataset": DATASET, "output": args.output, **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

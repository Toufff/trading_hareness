"""Import the latest stock-brain qfq research panel into PostgreSQL.

The source artifact is snapshotted under ``G:\\StockPlatform`` before import.
Adjusted research prices remain separate from canonical/raw execution bars.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row


MAINBOARD = re.compile(r"^(sh(?:600|601|603|605)|sz(?:000|001|002|003))\d{3}$")
REQUIRED = ("date", "symbol", "open", "high", "low", "close", "vol", "pct_chg")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    required = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"database settings missing: {', '.join(missing)}")
    return (
        f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
        f"dbname={os.environ['PGDATABASE']} user={os.environ['PGUSER']} "
        f"password={os.environ['PGPASSWORD']}"
    )


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalize_symbol(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not MAINBOARD.fullmatch(raw):
        return None
    return f"{raw[2:]}.{'SH' if raw.startswith('sh') else 'SZ'}"


def snapshot_source(source: Path, root: Path) -> tuple[Path, str]:
    digest = sha256(source)
    destination = root / "data" / "imports" / "adjusted-research-bars" / f"{source.stem}-{digest[:16]}.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(".tmp")
        shutil.copy2(source, temporary)
        if sha256(temporary) != digest:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("adjusted research artifact snapshot hash mismatch")
        temporary.replace(destination)
    return destination, digest


def import_bars(source: Path, platform_root: Path) -> dict[str, Any]:
    snapshot, digest = snapshot_source(source.resolve(), platform_root.resolve())
    frame = pd.read_parquet(snapshot, columns=list(REQUIRED))
    missing = sorted(set(REQUIRED) - set(frame.columns))
    if missing:
        raise RuntimeError(f"adjusted research panel missing columns: {', '.join(missing)}")
    frame["normalized_symbol"] = frame["symbol"].map(normalize_symbol)
    frame = frame[frame["normalized_symbol"].notna()].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame = frame[frame["date"].notna() & frame["close"].map(lambda value: (number(value) or 0) > 0)]
    frame = frame.sort_values(["normalized_symbol", "date"]).drop_duplicates(
        ["normalized_symbol", "date"], keep="last",
    )
    available_at = datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc)
    with psycopg.connect(db_url(), row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TEMP TABLE adjusted_bar_stage (
                    symbol text,trading_date date,open numeric,high numeric,low numeric,close numeric,
                    volume numeric,pct_change numeric
                ) ON COMMIT DROP
            """)
            with cursor.copy(
                "COPY adjusted_bar_stage(symbol,trading_date,open,high,low,close,volume,pct_change) FROM STDIN"
            ) as copy:
                for row in frame.itertuples(index=False):
                    copy.write_row((
                        row.normalized_symbol, row.date, number(row.open), number(row.high),
                        number(row.low), number(row.close), number(row.vol), number(row.pct_chg),
                    ))
            cursor.execute("""
                INSERT INTO quant.instruments(symbol,exchange,source)
                SELECT DISTINCT symbol,split_part(symbol,'.',2),'stock_brain_tencent_qfq'
                  FROM adjusted_bar_stage
                ON CONFLICT(symbol) DO NOTHING
            """)
            cursor.execute("""
                INSERT INTO quant.research_adjusted_bars_daily(
                    symbol,trading_date,open,high,low,close,volume,pct_change,
                    adjustment_basis,provider,source_artifact_sha256,source_available_at,metadata)
                SELECT symbol,trading_date,open,high,low,close,volume,pct_change,
                       'qfq','stock_brain_tencent_qfq',%s,%s,
                       jsonb_build_object(
                         'source_artifact',%s::text,
                         'contract','research_only_not_raw_execution_price',
                         'original_symbol_format','exchange_prefix')
                  FROM adjusted_bar_stage
                ON CONFLICT(symbol,trading_date,adjustment_basis,provider) DO UPDATE SET
                  open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
                  volume=EXCLUDED.volume,pct_change=EXCLUDED.pct_change,
                  source_artifact_sha256=EXCLUDED.source_artifact_sha256,
                  source_available_at=EXCLUDED.source_available_at,metadata=EXCLUDED.metadata,
                  imported_at=now()
            """, (digest, available_at, str(snapshot)))
            imported = cursor.rowcount
            cursor.execute("""
                SELECT count(*)::int AS rows,count(DISTINCT symbol)::int AS symbols,
                       min(trading_date) AS min_date,max(trading_date) AS max_date
                  FROM quant.research_adjusted_bars_daily
                 WHERE provider='stock_brain_tencent_qfq' AND source_artifact_sha256=%s
            """, (digest,))
            receipt = dict(cursor.fetchone())
        connection.commit()
    return {
        "status": "completed", "source_snapshot": str(snapshot), "source_sha256": digest,
        "upserted": imported, **receipt,
        "price_basis": "qfq_research_only_not_raw_execution_price",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--platform-root", type=Path, default=Path(r"G:\StockPlatform"))
    args = parser.parse_args()
    print(import_bars(args.source, args.platform_root))


if __name__ == "__main__":
    main()

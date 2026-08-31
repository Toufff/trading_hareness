#!/usr/bin/env python3
"""Read a symbol from whichever tier has it, warm first.

Local parquet answers in milliseconds; the pan copy costs about a second per
range request, so it is the fallback and the durable archive, not the default.
"""
from __future__ import annotations
import os, sys
import duckdb, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOME = os.path.expanduser('~')
CATALOG = os.path.join(HOME, 'marketdata', 'catalog', 'catalog.duckdb')


def load(symbol: str, dataset: str = 'daily', columns=None, prefer: str = 'warm') -> pd.DataFrame:
    con = duckdb.connect(CATALOG, read_only=True)
    row = con.execute(
        "SELECT local_path, pan_path, pan_fs_id, bytes FROM partitions "
        "WHERE dataset=? AND symbol=? AND partition_key='all'", [dataset, symbol]).fetchone()
    con.close()
    if not row:
        raise KeyError(f'{symbol} not in catalog')
    local, pan_path, fs_id, size = row

    if prefer == 'warm' and local and os.path.exists(local):
        import pyarrow.parquet as pq
        return pq.ParquetFile(local).read(columns=columns).to_pandas()

    if not fs_id:
        raise FileNotFoundError(f'{symbol} has no cold copy')
    import pan_client as pc
    import pyarrow.parquet as pq
    f = pc.PanFile(fs_id, size)
    return pq.ParquetFile(f).read(columns=columns).to_pandas()


def tiers() -> pd.DataFrame:
    con = duckdb.connect(CATALOG, read_only=True)
    df = con.execute("""
        SELECT symbol, rows, date_min, date_max,
               local_path IS NOT NULL AS warm,
               pan_fs_id IS NOT NULL  AS cold,
               round(bytes/1024.0) AS kb
        FROM partitions WHERE dataset='daily' ORDER BY symbol
    """).df()
    con.close()
    return df


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol', nargs='?')
    ap.add_argument('--cold', action='store_true', help='force the pan copy')
    ap.add_argument('--cols', default='date,close')
    args = ap.parse_args()
    if not args.symbol:
        print(tiers().to_string(index=False))
    else:
        df = load(args.symbol, columns=args.cols.split(','), prefer='cold' if args.cold else 'warm')
        print(f"{args.symbol}  {len(df)} rows  ({'cold/pan' if args.cold else 'warm/local'})")
        print(df.tail(5).to_string(index=False))

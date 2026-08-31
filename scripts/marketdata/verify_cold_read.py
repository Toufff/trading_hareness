#!/usr/bin/env python3
"""Prove a cold-tier parquet answers the same query as the local copy.

Deleting the warm copy is only safe if the pan copy is genuinely readable in
place, so this compares real query results rather than trusting the upload.
"""
from __future__ import annotations
import argparse, os, sys, time
import duckdb, pandas as pd, pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

CATALOG = os.path.expanduser('~/marketdata/catalog/catalog.duckdb')


def summarize(df: pd.DataFrame) -> dict:
    return {
        'rows': len(df),
        'symbols': int(df['symbol'].nunique()),
        'date_min': str(pd.to_datetime(df['trading_date']).min().date()),
        'date_max': str(pd.to_datetime(df['trading_date']).max().date()),
        'close_sum': round(float(pd.to_numeric(df['close'], errors='coerce').sum()), 2) if 'close' in df else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dataset', default='canonical_bars_daily')
    ap.add_argument('--year', default='2025')
    ap.add_argument('--symbol', default='600519.SH')
    args = ap.parse_args()

    con = duckdb.connect(CATALOG, read_only=True)
    row = con.execute("SELECT local_path, pan_path, pan_fs_id, bytes, rows FROM partitions "
                      "WHERE dataset=? AND symbol='__ALL__' AND partition_key=?",
                      [args.dataset, args.year]).fetchone()
    con.close()
    if not row:
        print(f'  {args.dataset}/{args.year} not in catalog'); return 2
    local, pan_path, fs_id, nbytes, nrows = row
    if not fs_id:
        print(f'  {args.dataset}/{args.year} has no cold copy yet'); return 2

    cols = ['symbol', 'trading_date', 'close']
    print(f'  dataset={args.dataset} year={args.year}  ({nrows:,} rows, {nbytes/1024/1024:.1f}MB)')

    warm = None
    if local and os.path.exists(local):
        t = time.time()
        warm = pq.ParquetFile(local).read(columns=cols).to_pandas()
        print(f'  warm(local): {summarize(warm)}  {(time.time()-t)*1000:.0f}ms')
    else:
        print('  warm(local): absent -- cold read is the only source')

    f = pc.PanFile(fs_id, nbytes)
    t = time.time()
    pf = pq.ParquetFile(f)
    cold = pf.read(columns=cols).to_pandas()
    el = time.time() - t
    print(f'  cold(pan) : {summarize(cold)}  {el:.1f}s, {f.reads} range requests')

    if warm is not None:
        same = summarize(warm) == summarize(cold)
        print(f'  identical : {"YES" if same else "NO"}')
        if not same:
            return 1

    # a realistic single-symbol query straight off the cold copy
    f2 = pc.PanFile(fs_id, nbytes)
    t = time.time()
    tbl = pq.ParquetFile(f2).read(columns=cols)
    sub = tbl.to_pandas()
    sub = sub[sub['symbol'] == args.symbol].sort_values('trading_date')
    print(f'  {args.symbol} from cold: {len(sub)} rows, '
          f'{sub["trading_date"].min()}~{sub["trading_date"].max()}, '
          f'last close={sub["close"].iloc[-1] if len(sub) else "n/a"}  ({time.time()-t:.1f}s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

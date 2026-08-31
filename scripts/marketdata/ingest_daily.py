#!/usr/bin/env python3
"""Fetch daily bars into the local warm tier and register them in the catalog.

One parquet file per symbol holding its full history, because a query almost
always wants one symbol across many years -- that keeps a Baidu Pan range read
down to a single file, which matters at ~1s per random read.
"""
from __future__ import annotations
import argparse, hashlib, os, sys, uuid
from datetime import datetime, date
import duckdb, pandas as pd

HOME = os.path.expanduser('~')
ROOT = os.path.join(HOME, 'marketdata')
CATALOG = os.path.join(ROOT, 'catalog', 'catalog.duckdb')
DAILY_DIR = os.path.join(ROOT, 'parquet', 'daily')
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,pbMRQ"

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def fetch(bs, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(symbol, FIELDS, start_date=start, end_date=end,
                                      frequency="d", adjustflag=adjust)
    if rs.error_code != '0':
        raise RuntimeError(f"baostock {rs.error_code}: {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=rs.fields)
    df['date'] = pd.to_datetime(df['date']).dt.date
    for c in ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn', 'pctChg', 'peTTM', 'pbMRQ']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.sort_values('date').reset_index(drop=True)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', help='comma separated, e.g. sz.000001,sh.600519')
    ap.add_argument('--watchlist', action='store_true', help='use active rows in catalog watchlist')
    ap.add_argument('--start', default='2015-01-01')
    ap.add_argument('--end', default=date.today().isoformat())
    ap.add_argument('--adjust', default='2', choices=['1', '2', '3'], help='1=hfq 2=qfq 3=none')
    args = ap.parse_args()

    con = duckdb.connect(CATALOG)
    if args.watchlist:
        symbols = [r[0] for r in con.execute("SELECT symbol FROM watchlist WHERE active ORDER BY symbol").fetchall()]
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        print('need --symbols or --watchlist', file=sys.stderr)
        return 2
    if not symbols:
        print('no symbols', file=sys.stderr)
        return 2

    os.makedirs(DAILY_DIR, exist_ok=True)
    adj_name = {'1': 'hfq', '2': 'qfq', '3': 'none'}[args.adjust]
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now()

    import baostock as bs
    lg = bs.login()
    if lg.error_code != '0':
        print(f'baostock login failed: {lg.error_msg}', file=sys.stderr)
        return 1

    ok = fail = 0
    try:
        for sym in symbols:
            try:
                df = fetch(bs, sym, args.start, args.end, args.adjust)
                if df.empty:
                    print(f'  {sym}: no rows'); fail += 1; continue
                path = os.path.join(DAILY_DIR, f'{sym}.parquet')
                # Large row groups keep a remote range read to few seeks.
                df.to_parquet(path, engine='pyarrow', compression='zstd', index=False,
                              row_group_size=len(df) if len(df) else None)
                st = os.stat(path)
                con.execute("""
                    INSERT INTO partitions (dataset,symbol,partition_key,local_path,rows,bytes,sha256,
                                            date_min,date_max,source,adjust,ingested_at)
                    VALUES ('daily',?,'all',?,?,?,?,?,?,'baostock',?,now())
                    ON CONFLICT (dataset,symbol,partition_key) DO UPDATE SET
                      local_path=EXCLUDED.local_path, rows=EXCLUDED.rows, bytes=EXCLUDED.bytes,
                      sha256=EXCLUDED.sha256, date_min=EXCLUDED.date_min, date_max=EXCLUDED.date_max,
                      adjust=EXCLUDED.adjust, ingested_at=now()
                """, [sym, path, len(df), st.st_size, sha256_file(path),
                      df['date'].min(), df['date'].max(), adj_name])
                print(f'  {sym}: {len(df)} rows  {df["date"].min()}~{df["date"].max()}  {st.st_size/1024:.0f}KB')
                ok += 1
            except Exception as e:
                print(f'  {sym}: FAIL {type(e).__name__}: {str(e)[:70]}'); fail += 1
    finally:
        bs.logout()

    con.execute("INSERT INTO ingest_runs VALUES (?,?,?,'daily',?,?,?)",
                [run_id, started, datetime.now(), ok, fail, f'{args.start}~{args.end} adj={adj_name}'])
    con.close()
    print(f'\n  run {run_id}: {ok} ok, {fail} fail')
    return 0 if fail == 0 else 1

if __name__ == '__main__':
    raise SystemExit(main())

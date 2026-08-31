#!/usr/bin/env python3
"""Push warm-tier parquet into the Baidu Pan cold tier and record it in the catalog.

The catalog keeps fs_id so a later read goes straight to filemetas/dlink without
listing a directory first.
"""
from __future__ import annotations
import argparse, os, sys, time
import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

HOME = os.path.expanduser('~')
CATALOG = os.path.join(HOME, 'marketdata', 'catalog', 'catalog.duckdb')
PAN_ROOT = '/apps/股票paper存储/market-data'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dataset', default='daily')
    ap.add_argument('--symbols', help='comma separated; default = every catalog row missing an upload')
    ap.add_argument('--force', action='store_true', help='re-upload even when already uploaded')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    con = duckdb.connect(CATALOG)
    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(',') if s.strip()]
        rows = con.execute(
            "SELECT symbol, local_path, sha256 FROM partitions WHERE dataset=? AND symbol IN ("
            + ",".join("?" * len(syms)) + ") ORDER BY symbol", [args.dataset, *syms]).fetchall()
    else:
        cond = "" if args.force else " AND (uploaded_at IS NULL OR pan_fs_id IS NULL)"
        rows = con.execute(
            f"SELECT symbol, local_path, sha256 FROM partitions WHERE dataset=?{cond} ORDER BY symbol",
            [args.dataset]).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print('  nothing to upload')
        return 0

    ok = fail = 0
    for sym, local, _sha in rows:
        if not local or not os.path.exists(local):
            print(f'  {sym}: local parquet missing'); fail += 1; continue
        pan_path = f'{PAN_ROOT}/{args.dataset}/{sym}.parquet'
        try:
            t = time.time()
            r = pc.upload(local, pan_path)
            fs_id = r.get('fs_id')
            con.execute("UPDATE partitions SET pan_path=?, pan_fs_id=?, uploaded_at=now() "
                        "WHERE dataset=? AND symbol=? AND partition_key='all'",
                        [pan_path, fs_id, args.dataset, sym])
            print(f'  {sym}: uploaded {os.path.getsize(local)/1024:.0f}KB in {time.time()-t:.1f}s  fs_id={fs_id}')
            ok += 1
        except Exception as e:
            print(f'  {sym}: FAIL {type(e).__name__}: {str(e)[:80]}'); fail += 1
    con.close()
    print(f'\n  {ok} uploaded, {fail} failed')
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())

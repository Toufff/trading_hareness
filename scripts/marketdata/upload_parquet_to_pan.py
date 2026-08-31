#!/usr/bin/env python3
"""Upload year-partitioned parquet to the cold tier and record fs_id in the catalog.

Parquet goes up unencrypted on purpose: market data is not sensitive, and leaving
it readable is what allows a range read to answer a query without downloading the
file. Anything sensitive (database dumps, credentials) is encrypted separately.
"""
from __future__ import annotations
import os, sys, time
import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

HOME = os.path.expanduser('~')
CATALOG = os.path.join(HOME, 'marketdata', 'catalog', 'catalog.duckdb')
PAN_ROOT = '/apps/股票paper存储/market-data'

con = duckdb.connect(CATALOG)
rows = con.execute("""
    SELECT dataset, partition_key, local_path, bytes FROM partitions
    WHERE symbol='__ALL__' AND (pan_fs_id IS NULL OR uploaded_at IS NULL)
    ORDER BY dataset, partition_key
""").fetchall()
if not rows:
    print('  nothing pending'); raise SystemExit(0)

print(f'  {len(rows)} file(s), {sum(r[3] for r in rows)/1024/1024:.0f}MB total')
ok = fail = 0
for dataset, pkey, local, nbytes in rows:
    if not local or not os.path.exists(local):
        print(f'  {dataset}/{pkey}: local missing'); fail += 1; continue
    pan_path = f'{PAN_ROOT}/{dataset}/{pkey}.parquet'
    try:
        t = time.time()
        r = pc.upload(local, pan_path)
        con.execute("UPDATE partitions SET pan_path=?, pan_fs_id=?, uploaded_at=now() "
                    "WHERE dataset=? AND symbol='__ALL__' AND partition_key=?",
                    [pan_path, r.get('fs_id'), dataset, pkey])
        print(f'  {dataset}/{pkey}: {nbytes/1024/1024:5.1f}MB  {time.time()-t:5.1f}s  fs_id={r.get("fs_id")}')
        ok += 1
    except Exception as e:
        print(f'  {dataset}/{pkey}: FAIL {type(e).__name__}: {str(e)[:70]}'); fail += 1
con.close()
print(f'\n  {ok} uploaded, {fail} failed')

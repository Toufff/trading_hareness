#!/usr/bin/env python3
"""Upload per-symbol parquet to the cold tier in parallel.

Per-symbol files exist because a single-stock study is the common research
question, and a whole-year file makes that read pull roughly 750 unrelated
symbols. One small file answers it in a single range request instead.
"""
from __future__ import annotations
import os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

HOME = os.path.expanduser('~')
SRC = os.path.join(HOME, 'marketdata', 'parquet', 'by_symbol')
CATALOG = os.path.join(HOME, 'marketdata', 'catalog', 'catalog.duckdb')
PAN_DIR = '/apps/股票paper存储/market-data/by_symbol'
WORKERS = int(os.environ.get('PAN_UPLOAD_WORKERS', '6'))

done_lock = threading.Lock()
counter = {'ok': 0, 'fail': 0}


def existing_on_pan() -> dict[str, int]:
    """One listing beats 5662 stat calls."""
    found, start = {}, 0
    while True:
        try:
            r = pc._get('/rest/2.0/xpan/file', {'method': 'list', 'dir': PAN_DIR, 'limit': 1000, 'start': start})
        except Exception:
            return found
        items = r.get('list', [])
        for it in items:
            if not it.get('isdir'):
                found[it['server_filename']] = it['size']
        if len(items) < 1000:
            return found
        start += 1000


def upload_one(name: str):
    local = os.path.join(SRC, name)
    try:
        r = pc.upload(local, f'{PAN_DIR}/{name}')
        with done_lock:
            counter['ok'] += 1
            if counter['ok'] % 250 == 0:
                print(f"    {counter['ok']} uploaded...", flush=True)
        return name, r.get('fs_id'), os.path.getsize(local), None
    except Exception as e:
        with done_lock:
            counter['fail'] += 1
        return name, None, 0, str(e)[:80]


def main() -> int:
    names = sorted(n for n in os.listdir(SRC) if n.endswith('.parquet'))
    print(f'  {len(names)} per-symbol files locally')
    already = existing_on_pan()
    pending = [n for n in names if already.get(n) != os.path.getsize(os.path.join(SRC, n))]
    print(f'  {len(already)} already on pan; {len(pending)} to upload with {WORKERS} workers')
    if not pending:
        return 0

    results, started = [], time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(upload_one, n) for n in pending]
        for f in as_completed(futs):
            results.append(f.result())

    elapsed = time.time() - started
    print(f"  uploaded {counter['ok']}, failed {counter['fail']}, {elapsed/60:.1f} min")

    con = duckdb.connect(CATALOG)
    rows = [(n[:-len('.parquet')], f'{PAN_DIR}/{n}', fs, sz) for n, fs, sz, err in results if fs]
    for sym, pan_path, fs_id, nbytes in rows:
        con.execute("""
            INSERT INTO partitions (dataset,symbol,partition_key,local_path,pan_path,pan_fs_id,bytes,
                                    source,adjust,ingested_at,uploaded_at)
            VALUES ('daily_by_symbol', ?, 'all', ?, ?, ?, ?, 'research_pg', 'canonical', now(), now())
            ON CONFLICT (dataset,symbol,partition_key) DO UPDATE SET
              pan_path=EXCLUDED.pan_path, pan_fs_id=EXCLUDED.pan_fs_id,
              bytes=EXCLUDED.bytes, uploaded_at=now()
        """, [sym, os.path.join(SRC, sym + '.parquet'), pan_path, fs_id, nbytes])
    con.close()
    print(f'  catalog updated for {len(rows)} symbols')

    bad = [(n, e) for n, fs, sz, e in results if e]
    for n, e in bad[:5]:
        print(f'    FAILED {n}: {e}')
    return 0 if not bad else 1


if __name__ == '__main__':
    raise SystemExit(main())

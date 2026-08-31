#!/usr/bin/env python3
"""Upload already-produced backup chunks, skipping any that are already on pan.

Kept separate from the dump so a failed upload never costs another 6-minute
pg_dump, and so a partial run can simply be re-run.
"""
from __future__ import annotations
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

stage = os.path.expanduser('~/marketdata/tmp/pgbackup')
stamp = sys.argv[1] if len(sys.argv) > 1 else '20260831'
base = f'/apps/股票paper存储/db-backups/research-pg/{stamp}'

names = sorted(n for n in os.listdir(stage) if n.startswith(('dump-', 'manifest-')))
print(f'  {len(names)} file(s) to place under {base}')
for name in names:
    local = os.path.join(stage, name)
    size = os.path.getsize(local)
    try:
        existing = pc.stat(f'{base}/{name}')
    except Exception:
        existing = None
    if existing and existing.get('size') == size:
        print(f'  {name}: already on pan ({size/1024/1024:.0f}MB), skipped')
        continue
    t = time.time()
    def show(done, total, _n=name):
        print(f'    {_n}: slice {done}/{total}', flush=True)
    r = pc.upload(local, f'{base}/{name}', progress=show)
    print(f'  {name}: {size/1024/1024:.0f}MB in {time.time()-t:.0f}s  fs_id={r.get("fs_id")}')
print('  done')

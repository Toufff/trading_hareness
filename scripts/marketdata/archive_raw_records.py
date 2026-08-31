#!/usr/bin/env python3
"""Archive tushare_raw_records to the cold tier, partitioned by api_name and year.

This one table is 13GB -- 45% of the hot database -- and the storage guard
starts refusing optional high-frequency capture as the database approaches its
36GiB soft limit. Moving its history out is what buys room to widen every
retention window.

Partitioned by api_name because that is how the 42 call sites filter it, and by
year to keep each export inside a sane amount of memory. Rows are only ever
deleted after the uploaded parquet has been read back and its row count matched.
"""
from __future__ import annotations
import argparse, io, os, subprocess, sys, time
import duckdb, pandas as pd, pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

HOME = os.path.expanduser('~')
ROOT = os.path.join(HOME, 'marketdata')
CATALOG = os.path.join(ROOT, 'catalog', 'catalog.duckdb')
OUTDIR = os.path.join(ROOT, 'parquet', 'tushare_raw_records')
PAN_ROOT = '/apps/股票paper存储/evidence-archive/tushare_raw_records'
COMPOSE = ['/opt/homebrew/bin/docker', 'compose', '-f', os.path.join(HOME, 'codebase/n8n/compose.yaml')]
TABLE = 'quant.tushare_raw_records'


def psql(sql: str) -> str:
    p = subprocess.run(COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'n8n', '-d', 'n8n',
                                  '-v', 'ON_ERROR_STOP=1', '-P', 'pager=off', '-tAc', sql],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return p.stdout.decode().strip()


def psql_csv(sql: str) -> pd.DataFrame:
    p = subprocess.run(COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'n8n', '-d', 'n8n',
                                  '-v', 'ON_ERROR_STOP=1',
                                  '-c', f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)"],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return pd.read_csv(io.BytesIO(p.stdout), low_memory=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep-days', type=int, default=90,
                    help='rows newer than this stay hot; only older ones may be pruned')
    ap.add_argument('--archive-only', action='store_true', help='upload and verify, keep every row')
    ap.add_argument('--apply', action='store_true', help='prune archived rows after verification')
    ap.add_argument('--api', help='restrict to one api_name')
    args = ap.parse_args()

    cutoff = psql(f"SELECT (CURRENT_DATE - {args.keep_days})::text")
    where_api = f" AND api_name = '{args.api}'" if args.api else ''

    combos = psql(
        f"SELECT api_name || ' ' || EXTRACT(year FROM available_at)::int || ' ' || count(*) "
        f"FROM {TABLE} WHERE available_at < DATE '{cutoff}'{where_api} "
        f"GROUP BY api_name, EXTRACT(year FROM available_at) "
        f"ORDER BY api_name, EXTRACT(year FROM available_at)")
    rows = [l.split() for l in combos.splitlines() if l.strip()]
    if not rows:
        print('  nothing older than the keep window'); return 0

    total = sum(int(r[2]) for r in rows)
    print(f'  cutoff {cutoff}: {total:,} rows across {len(rows)} (api_name, year) partitions')
    for api, year, n in rows:
        print(f'    {api:22s} {year}  {int(n):>9,}')
    if not (args.archive_only or args.apply):
        print('  dry run; pass --archive-only or --apply'); return 0

    os.makedirs(OUTDIR, exist_ok=True)
    con = duckdb.connect(CATALOG)
    archived = pruned = 0

    for api, year, n in rows:
        key = f'{api}_{year}'
        lo, hi = f'{year}-01-01', f'{int(year)+1}-01-01'
        upper = min(hi, cutoff)
        t = time.time()
        df = psql_csv(
            f"SELECT record_id, api_name, request_key, record_index, record_key, content_sha256, "
            f"row_data::text AS row_data, available_at, created_at, provider_key, ingested_at, availability_basis "
            f"FROM {TABLE} WHERE api_name='{api}' "
            f"AND available_at >= DATE '{lo}' AND available_at < DATE '{upper}'")
        if df.empty:
            continue
        path = os.path.join(OUTDIR, f'{key}.parquet')
        df.to_parquet(path, engine='pyarrow', compression='zstd', index=False, row_group_size=100_000)
        nbytes = os.path.getsize(path)
        pan_path = f'{PAN_ROOT}/{key}.parquet'

        # Upload and read-back both talk to pan over a link that drops often
        # enough to matter across 42 partitions; a transient TLS reset must cost
        # one retry, not the rest of the run.
        fs_id = None
        for attempt in range(3):
            try:
                fs_id = pc.upload(path, pan_path).get('fs_id')
                back = pq.ParquetFile(pc.PanFile(fs_id, nbytes)).metadata.num_rows
                if back != len(df):
                    print(f'  {key}: VERIFY FAILED (pan {back} vs local {len(df)}); skipping')
                    fs_id = None
                break
            except Exception as error:  # noqa: BLE001 - retried, then skipped
                print(f'  {key}: attempt {attempt + 1} failed ({type(error).__name__}); retrying')
                fs_id = None
                time.sleep(5 * (attempt + 1))
        if fs_id is None:
            print(f'  {key}: not archived, left in PostgreSQL')
            continue

        con.execute("""
            INSERT INTO partitions (dataset,symbol,partition_key,local_path,pan_path,pan_fs_id,rows,bytes,
                                    source,adjust,ingested_at,uploaded_at)
            VALUES ('tushare_raw_records', ?, ?, ?, ?, ?, ?, ?, 'research_pg', 'raw', now(), now())
            ON CONFLICT (dataset,symbol,partition_key) DO UPDATE SET
              local_path=EXCLUDED.local_path, pan_path=EXCLUDED.pan_path, pan_fs_id=EXCLUDED.pan_fs_id,
              rows=EXCLUDED.rows, bytes=EXCLUDED.bytes, uploaded_at=now()
        """, [api, str(year), path, pan_path, fs_id, len(df), nbytes])
        archived += len(df)
        print(f'  {key}: {len(df):,} rows -> {nbytes/1024/1024:.1f}MB, verified on pan  ({time.time()-t:.0f}s)')

        if args.apply:
            got = psql(f"WITH d AS (DELETE FROM {TABLE} WHERE api_name='{api}' "
                       f"AND available_at >= DATE '{lo}' AND available_at < DATE '{upper}' RETURNING 1) "
                       f"SELECT count(*) FROM d")
            pruned += int(got)
            print(f'         pruned {got} rows from PostgreSQL')
        # free the frame before the next partition
        del df

    con.close()
    size = psql(f"SELECT pg_size_pretty(pg_total_relation_size('{TABLE}'))")
    left = psql(f"SELECT count(*) FROM {TABLE}")
    print(f'\n  archived {archived:,} rows; pruned {pruned:,}')
    print(f'  {TABLE} now {left} rows ({size})')
    if pruned:
        print('  run VACUUM FULL on that table to return the freed pages to disk')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

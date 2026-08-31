#!/usr/bin/env python3
"""Archive old evidence to the cold tier, then prune it from PostgreSQL.

The research database has no retention of its own -- the edge prunes to stay
bounded, but the workstation keeps everything, which is why it reached 28GB.
This moves a closed time window out to pan and only deletes the rows once the
uploaded parquet has been read back successfully, so a failed upload can never
cost history.

Nothing is pruned without --apply; the default run reports what it would do.
"""
from __future__ import annotations
import argparse, os, subprocess, sys, io, time
from datetime import date, timedelta
import duckdb, pandas as pd, pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pan_client as pc

HOME = os.path.expanduser('~')
ROOT = os.path.join(HOME, 'marketdata')
CATALOG = os.path.join(ROOT, 'catalog', 'catalog.duckdb')
PAN_ROOT = '/apps/股票paper存储/evidence-archive'
COMPOSE = ['/opt/homebrew/bin/docker', 'compose', '-f', os.path.join(HOME, 'codebase/n8n/compose.yaml')]

# table -> (timestamp column, default days to keep hot)
ARCHIVABLE = {
    'tushare_raw_records':           ('available_at', 180),
    'intraday_quote_observations':   ('observed_at', 90),
    'intraday_rule_input_snapshots': ('captured_at', 90),
    'raw_market_observations':       ('available_at', 180),
    # Minute bars are the scarcest evidence the intraday rules replay against,
    # so they are archived for durability rather than to reclaim space.
    'market_bars_minute':            ('bar_time', 3650),
}


def psql(sql: str, capture=True):
    cmd = COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'n8n', '-d', 'n8n',
                     '-v', 'ON_ERROR_STOP=1', '-P', 'pager=off', '-tAc', sql]
    p = subprocess.run(cmd, capture_output=capture)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return p.stdout.decode().strip()


def psql_csv(sql: str) -> pd.DataFrame:
    cmd = COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'n8n', '-d', 'n8n', '-v', 'ON_ERROR_STOP=1',
                     '-c', f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return pd.read_csv(io.BytesIO(p.stdout), low_memory=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', required=True, choices=sorted(ARCHIVABLE))
    ap.add_argument('--keep-days', type=int)
    ap.add_argument('--apply', action='store_true', help='actually delete after a verified upload')
    ap.add_argument('--archive-only', action='store_true',
                    help='upload and verify but keep every row in PostgreSQL')
    args = ap.parse_args()

    tscol, default_days = ARCHIVABLE[args.table]
    keep = args.keep_days if args.keep_days is not None else default_days
    cutoff = date.today() + timedelta(days=1) if '--archive-only' in sys.argv else date.today() - timedelta(days=keep)

    total = int(psql(f"SELECT count(*) FROM quant.{args.table}"))
    old = int(psql(f"SELECT count(*) FROM quant.{args.table} WHERE {tscol} < DATE '{cutoff}'"))
    size = psql(f"SELECT pg_size_pretty(pg_total_relation_size('quant.{args.table}'))")
    print(f'  {args.table}: {total:,} rows ({size}); {old:,} older than {cutoff} (keep {keep}d)')
    if old == 0:
        print('  nothing to archive'); return 0

    years = [int(y) for y in psql(
        f"SELECT DISTINCT EXTRACT(year FROM {tscol})::int FROM quant.{args.table} "
        f"WHERE {tscol} < DATE '{cutoff}' ORDER BY 1").split()]
    print(f'  year partitions to archive: {years}')
    if not (args.apply or args.archive_only):
        print('  dry run; pass --archive-only to back up, or --apply to also prune'); return 0

    con = duckdb.connect(CATALOG)
    outdir = os.path.join(ROOT, 'parquet', args.table)
    os.makedirs(outdir, exist_ok=True)

    for year in years:
        lo, hi = f'{year}-01-01', f'{year+1}-01-01'
        upper = min(pd.Timestamp(hi), pd.Timestamp(cutoff)).date().isoformat()
        df = psql_csv(f"SELECT * FROM quant.{args.table} "
                      f"WHERE {tscol} >= DATE '{lo}' AND {tscol} < DATE '{upper}' ORDER BY {tscol}")
        if df.empty:
            continue
        path = os.path.join(outdir, f'{year}.parquet')
        df.to_parquet(path, engine='pyarrow', compression='zstd', index=False, row_group_size=200_000)
        nbytes = os.path.getsize(path)
        pan_path = f'{PAN_ROOT}/{args.table}/{year}.parquet'
        r = pc.upload(path, pan_path)
        fs_id = r.get('fs_id')

        # Read it back from pan before deleting anything.
        check = pq.ParquetFile(pc.PanFile(fs_id, nbytes)).metadata.num_rows
        if check != len(df):
            print(f'  {year}: VERIFY FAILED (pan {check} vs local {len(df)}); not pruning'); return 1
        con.execute("""
            INSERT INTO partitions (dataset,symbol,partition_key,local_path,pan_path,pan_fs_id,
                                    rows,bytes,source,adjust,ingested_at,uploaded_at)
            VALUES (?, '__ALL__', ?, ?, ?, ?, ?, ?, 'research_pg', 'evidence', now(), now())
            ON CONFLICT (dataset,symbol,partition_key) DO UPDATE SET
              local_path=EXCLUDED.local_path, pan_path=EXCLUDED.pan_path, pan_fs_id=EXCLUDED.pan_fs_id,
              rows=EXCLUDED.rows, bytes=EXCLUDED.bytes, uploaded_at=now()
        """, [args.table, str(year), path, pan_path, fs_id, len(df), nbytes])

        if args.archive_only:
            print(f'  {year}: archived {len(df):,} rows ({nbytes/1024/1024:.1f}MB), verified on pan, kept in PG')
            continue
        deleted = psql(f"WITH d AS (DELETE FROM quant.{args.table} "
                       f"WHERE {tscol} >= DATE '{lo}' AND {tscol} < DATE '{upper}' RETURNING 1) "
                       f"SELECT count(*) FROM d")
        print(f'  {year}: archived {len(df):,} rows ({nbytes/1024/1024:.1f}MB), verified on pan, pruned {deleted}')

    con.close()
    after = psql(f"SELECT pg_size_pretty(pg_total_relation_size('quant.{args.table}'))")
    remaining = psql(f"SELECT count(*) FROM quant.{args.table}")
    print(f'  {args.table} now {remaining} rows ({after}); run VACUUM FULL to return the space to disk')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

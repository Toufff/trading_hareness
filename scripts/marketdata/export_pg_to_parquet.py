#!/usr/bin/env python3
"""Export daily research tables from PostgreSQL into the warm parquet tier.

Partitioned by year and sorted by (symbol, trading_date): a query for one symbol
then touches only the row groups whose statistics cover it, which is what keeps a
Baidu Pan read down to a couple of range requests at roughly a second each.
"""
from __future__ import annotations
import argparse, hashlib, io, os, subprocess, sys
from datetime import datetime
import duckdb, pandas as pd

HOME = os.path.expanduser('~')
ROOT = os.path.join(HOME, 'marketdata')
CATALOG = os.path.join(ROOT, 'catalog', 'catalog.duckdb')
COMPOSE = ['/opt/homebrew/bin/docker', 'compose', '-f', os.path.join(HOME, 'codebase/n8n/compose.yaml')]

TABLES = {
    'canonical_bars_daily': 'trading_date',
    'daily_fundamentals': 'trading_date',
    'daily_trade_limits': 'trading_date',
    'daily_adjustment_factors': 'trading_date',
}


def psql_csv(sql: str) -> pd.DataFrame:
    cmd = COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'n8n', '-d', 'n8n', '-v', 'ON_ERROR_STOP=1',
                     '-c', f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return pd.read_csv(io.BytesIO(p.stdout), low_memory=False)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--tables', default=','.join(TABLES))
    ap.add_argument('--years', default='', help='e.g. 2024,2025; default = all present')
    args = ap.parse_args()

    con = duckdb.connect(CATALOG)
    tables = [t.strip() for t in args.tables.split(',') if t.strip()]

    for table in tables:
        datecol = TABLES.get(table, 'trading_date')
        span = psql_csv(f"SELECT min(EXTRACT(year FROM {datecol}))::int y0, max(EXTRACT(year FROM {datecol}))::int y1 FROM quant.{table}")
        y0, y1 = int(span.iloc[0]['y0']), int(span.iloc[0]['y1'])
        years = [int(y) for y in args.years.split(',') if y.strip()] or list(range(y0, y1 + 1))
        outdir = os.path.join(ROOT, 'parquet', table)
        os.makedirs(outdir, exist_ok=True)
        print(f'\n{table}  years {y0}..{y1}')

        for year in years:
            df = psql_csv(
                f"SELECT * FROM quant.{table} "
                f"WHERE {datecol} >= DATE '{year}-01-01' AND {datecol} < DATE '{year+1}-01-01' "
                f"ORDER BY symbol, {datecol}")
            if df.empty:
                print(f'  {year}: empty'); continue
            path = os.path.join(outdir, f'{year}.parquet')
            df.to_parquet(path, engine='pyarrow', compression='zstd', index=False, row_group_size=200_000)
            st = os.stat(path)
            con.execute("""
                INSERT INTO partitions (dataset,symbol,partition_key,local_path,rows,bytes,sha256,
                                        date_min,date_max,source,adjust,ingested_at)
                VALUES (?, '__ALL__', ?, ?, ?, ?, ?, ?, ?, 'research_pg', 'canonical', now())
                ON CONFLICT (dataset,symbol,partition_key) DO UPDATE SET
                  local_path=EXCLUDED.local_path, rows=EXCLUDED.rows, bytes=EXCLUDED.bytes,
                  sha256=EXCLUDED.sha256, date_min=EXCLUDED.date_min, date_max=EXCLUDED.date_max,
                  ingested_at=now()
            """, [table, str(year), path, len(df), st.st_size, sha256_file(path),
                  pd.to_datetime(df[datecol]).min().date(), pd.to_datetime(df[datecol]).max().date()])
            print(f'  {year}: {len(df):>9,} rows  {st.st_size/1024/1024:6.1f}MB  {df["symbol"].nunique()} symbols')
    con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

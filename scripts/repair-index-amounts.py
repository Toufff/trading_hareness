"""Restore index amounts only from selected-source immutable evidence. Default: dry-run."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.db_dsn import connection_params
from app.index_amount_repair import repair_index_amounts
import psycopg
from psycopg.rows import dict_row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--from', dest='start', type=date.fromisoformat, required=True)
    p.add_argument('--to', dest='end', type=date.fromisoformat, required=True)
    p.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--database')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--report')
    a = p.parse_args()
    if a.end < a.start or (a.end-a.start).days > 365:
        p.error('require an ordered window of at most 365 days')
    for line in Path(a.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            os.environ[key.strip()] = value.strip()
    if a.database:
        os.environ['PGDATABASE'] = a.database
    options = '-c statement_timeout=60000 -c lock_timeout=3000'
    if not a.apply:
        options += ' -c default_transaction_read_only=on'
    with psycopg.connect(**connection_params(), row_factory=dict_row, options=options) as conn:
        result = repair_index_amounts(conn, start=a.start, end=a.end, apply=a.apply)
    if a.report:
        Path(a.report).write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'corrections'}, default=str))


if __name__ == '__main__':
    main()

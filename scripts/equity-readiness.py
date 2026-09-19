"""Read a requested equity session from the sole production PostgreSQL store."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.daily_control_plane import status_query, status_payload
from app.db_dsn import connection_params
from app.settled_limit_pool_repository import settled_limit_pool_payload, settled_limit_pool_query


def main():
    # The reason text names exchanges and universe sources in Chinese; a
    # console codepage must not turn a readiness probe into a UnicodeEncodeError.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date', type=date.fromisoformat)
    p.add_argument('--env-file', default=r'G:\StockPlatform\config\runtime.env')
    a = p.parse_args()
    config = dict(line.split('=', 1) for line in Path(a.env_file).read_text(encoding='utf-8-sig').splitlines()
                  if '=' in line and not line.startswith('#'))
    with psycopg.connect(**connection_params(config), row_factory=dict_row, connect_timeout=10,
                        options='-c default_transaction_read_only=on -c statement_timeout=30000') as c:
        sql, params = status_query(a.date)
        payload = {'daily_control_plane': status_payload(c.execute(sql, params).fetchall())}
        # The close limit pool is derived from the bars above rather than fetched,
        # so its expected size is a property of this same cross-section. Reporting
        # both numbers lets the pipeline runner refuse to call the date landed
        # while the derived pool is still short, instead of trusting a clock.
        # Undated invocations have no session to compare against and get no block.
        if a.date is not None:
            pool_sql, pool_params = settled_limit_pool_query(a.date)
            payload['late_datasets'] = settled_limit_pool_payload(
                c.execute(pool_sql, pool_params).fetchone(), a.date)
        # ASCII-only on purpose: run-post-close-pipeline.ps1 parses this stdout
        # with ConvertFrom-Json under whatever console codepage the scheduled
        # task host gives pwsh (GBK on this machine). Raw UTF-8 Chinese in the
        # reason text was decoded as GBK there and broke the JSON string
        # (2026-09-18 20:40 preflight failure); \uXXXX escapes survive any codepage.
        print(json.dumps(payload, ensure_ascii=True))


if __name__ == '__main__':
    main()

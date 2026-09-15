"""Execute readiness SQL against isolated PostgreSQL fixtures and real read-only data."""
import argparse
import json
from pathlib import Path
import sys
import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.daily_control_plane import EQUITY_DAILY_CONTROL_STATUS_SQL, status_payload, status_query
from app.db_dsn import connection_params


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default=r'G:\StockPlatform\config\runtime.env')
    args = parser.parse_args()
    config = dict(line.split('=', 1) for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#'))
    with psycopg.connect(**connection_params(config), row_factory=dict_row, connect_timeout=10) as db:
        db.execute('SET statement_timeout=30000')
        db.execute('CREATE TEMP TABLE canonical_bars_daily(symbol text,trading_date date,quality_status text,adj_factor numeric,limit_up numeric,limit_down numeric)')
        db.execute('CREATE TEMP TABLE universe_membership_history(universe_key text,symbol text,effective_from date,effective_to date)')
        sql = EQUITY_DAILY_CONTROL_STATUS_SQL.replace('quant.', 'pg_temp.')
        assert status_payload(db.execute(sql).fetchone())['trade_date'] is None
        db.execute("INSERT INTO universe_membership_history VALUES ('all_a','600664.SH','2026-09-01',NULL)")
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600664.SH','2026-09-04','fresh',1,11,9),('000001.SH','2026-09-09','fresh',NULL,NULL,NULL)")
        result = status_payload(db.execute(sql).fetchone())
        assert result['trade_date'] == '2026-09-04' and result['daily_rows'] == 1, result
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600664.SH','2026-09-09','fresh',1,11,9)")
        assert status_payload(db.execute(sql).fetchone())['trade_date'] == '2026-09-09'
        dated_sql, params = status_query(__import__('datetime').date(2026,9,4))
        historical = status_payload(db.execute(dated_sql.replace('quant.', 'pg_temp.'), params).fetchone())
        assert historical['trade_date']=='2026-09-04' and historical['state']=='ready', historical
        db.rollback()  # all fixtures disappear, production tables untouched
        db.execute('SET TRANSACTION READ ONLY')
        actual = status_payload(db.execute(EQUITY_DAILY_CONTROL_STATUS_SQL).fetchone())
        print(json.dumps({'isolated_postgresql_cases_passed': 4, 'actual_equity_readiness': actual}, ensure_ascii=False))
        db.rollback()


if __name__ == '__main__':
    main()

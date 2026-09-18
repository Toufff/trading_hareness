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
        db.execute('CREATE TEMP TABLE canonical_bars_daily(symbol text,trading_date date,quality_status text,adj_factor numeric,limit_up numeric,limit_down numeric,selected_provider text)')
        db.execute('CREATE TEMP TABLE universe_membership_history(universe_key text,symbol text,effective_from date,effective_to date,source text)')
        sql = EQUITY_DAILY_CONTROL_STATUS_SQL.replace('quant.', 'pg_temp.')
        assert status_payload(db.execute(sql).fetchall())['trade_date'] is None
        db.execute("INSERT INTO universe_membership_history VALUES ('all_a','600664.SH','2026-09-01',NULL,'longhuvip_composite')")
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600664.SH','2026-09-04','fresh',1,11,9,'tushare_primary'),('000001.SH','2026-09-09','fresh',NULL,NULL,NULL,'tushare_primary')")
        result = status_payload(db.execute(sql).fetchall())
        assert result['trade_date'] == '2026-09-04' and result['daily_rows'] == 1, result
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600664.SH','2026-09-09','fresh',1,11,9,'tushare_primary')")
        assert status_payload(db.execute(sql).fetchall())['trade_date'] == '2026-09-09'
        dated_sql, params = status_query(__import__('datetime').date(2026,9,4))
        historical = status_payload(db.execute(dated_sql.replace('quant.', 'pg_temp.'), params).fetchall())
        assert historical['trade_date']=='2026-09-04' and historical['state']=='ready', historical
        assert historical['adjustment_state']=='complete', historical
        # A vendor-sourced session with no cumulative factor yet: the equity gate
        # stays ready and the factor lane is labelled 'pending', not 'blocked'.
        db.execute("INSERT INTO universe_membership_history VALUES ('all_a','600001.SH','2026-09-01',NULL,'longhuvip_composite')")
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600001.SH','2026-09-15','fresh',NULL,11,9,'longhuvip_composite'),('600664.SH','2026-09-15','fresh',12.5,11,9,'tushare_super_get')")
        pending_sql, pending_params = status_query(__import__('datetime').date(2026,9,15))
        pending = status_payload(db.execute(pending_sql.replace('quant.', 'pg_temp.'), pending_params).fetchall())
        assert pending['state']=='ready' and pending['adjustment_state']=='pending', pending
        assert pending['adjustment_pending_rows']==1 and not pending['research_adjustment_ready'], pending
        db.rollback()  # all fixtures disappear, production tables untouched
        db.execute('SET TRANSACTION READ ONLY')
        actual = status_payload(db.execute(EQUITY_DAILY_CONTROL_STATUS_SQL).fetchall())
        print(json.dumps({'isolated_postgresql_cases_passed': 5, 'actual_equity_readiness': actual}, ensure_ascii=False))
        db.rollback()


if __name__ == '__main__':
    main()

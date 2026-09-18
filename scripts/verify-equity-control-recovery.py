"""Execute readiness SQL against isolated PostgreSQL fixtures and real read-only data."""
import argparse
import json
from pathlib import Path
import sys
import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.adjustment_factor_maintenance import (
    BLOCKED_DATE_TASK_KEY,
    MAX_CONSECUTIVE_BLOCKED_RUNS,
    RETIRED_DATES_SQL,
    blocked_date_run_key,
    retired_date_details_from_rows,
)
from app.daily_control_plane import (
    EQUITY_DAILY_CONTROL_STATUS_SQL,
    adjustment_retirement_details,
    status_payload,
    status_query,
)
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
        assert status_payload(db.execute(sql).fetchall(), retired_dates={})['trade_date'] is None
        db.execute("INSERT INTO universe_membership_history VALUES ('all_a','600664.SH','2026-09-01',NULL,'longhuvip_composite')")
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600664.SH','2026-09-04','fresh',1,11,9,'tushare_primary'),('000001.SH','2026-09-09','fresh',NULL,NULL,NULL,'tushare_primary')")
        result = status_payload(db.execute(sql).fetchall(), retired_dates={})
        assert result['trade_date'] == '2026-09-04' and result['daily_rows'] == 1, result
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600664.SH','2026-09-09','fresh',1,11,9,'tushare_primary')")
        assert status_payload(db.execute(sql).fetchall(), retired_dates={})['trade_date'] == '2026-09-09'
        dated_sql, params = status_query(__import__('datetime').date(2026,9,4))
        historical = status_payload(db.execute(dated_sql.replace('quant.', 'pg_temp.'), params).fetchall(),
                                    retired_dates={})
        assert historical['trade_date']=='2026-09-04' and historical['state']=='ready', historical
        assert historical['adjustment_state']=='complete', historical
        # A vendor-sourced session with no cumulative factor yet: the equity gate
        # stays ready and the factor lane is labelled 'pending', not 'blocked'.
        db.execute("INSERT INTO universe_membership_history VALUES ('all_a','600001.SH','2026-09-01',NULL,'longhuvip_composite')")
        db.execute("INSERT INTO canonical_bars_daily VALUES ('600001.SH','2026-09-15','fresh',NULL,11,9,'longhuvip_composite'),('600664.SH','2026-09-15','fresh',12.5,11,9,'tushare_super_get')")
        pending_sql, pending_params = status_query(__import__('datetime').date(2026,9,15))
        pending = status_payload(db.execute(pending_sql.replace('quant.', 'pg_temp.'), pending_params).fetchall(),
                                 retired_dates={})  # no ledger row yet: this date is still queued
        assert pending['state']=='ready' and pending['adjustment_state']=='pending', pending
        assert pending['adjustment_pending_rows']==1 and not pending['research_adjustment_ready'], pending
        assert pending['adjustment_retirement'] is None, pending
        # The SAME date once the blocked-date ledger has retired it: refused on
        # MAX_CONSECUTIVE_BLOCKED_RUNS separate days, so no repair is queued and
        # the control plane must say 'retired' (with the run_key an operator
        # clears), never 'pending'. The ledger row lives in an isolated
        # pg_temp.automation_runs and is read by the production query and the
        # production row->details mapper, not by a hand-built dict.
        run_key = blocked_date_run_key(__import__('datetime').date(2026,9,15))
        db.execute('CREATE TEMP TABLE automation_runs(task_key text,run_key text,as_of_date date,output_summary jsonb)')
        db.execute('INSERT INTO automation_runs VALUES(%s,%s,%s,%s::jsonb)', (
            BLOCKED_DATE_TASK_KEY, run_key, __import__('datetime').date(2026,9,15),
            json.dumps({'consecutive_blocked_runs': MAX_CONSECUTIVE_BLOCKED_RUNS,
                        'reason': 'daily cross-section below the coverage gate',
                        'blocked_days': ['2026-09-15','2026-09-16','2026-09-17','2026-09-18','2026-09-19']})))
        ledger = retired_date_details_from_rows(db.execute(
            RETIRED_DATES_SQL.replace('quant.', 'pg_temp.'),
            (BLOCKED_DATE_TASK_KEY, [run_key], MAX_CONSECUTIVE_BLOCKED_RUNS)).fetchall())
        assert list(ledger) == [__import__('datetime').date(2026,9,15)], ledger
        retired = status_payload(db.execute(pending_sql.replace('quant.', 'pg_temp.'), pending_params).fetchall(),
                                 retired_dates={str(k): v for k, v in ledger.items()})
        assert retired['state']=='ready' and retired['adjustment_state']=='retired', retired
        assert retired['adjustment_retirement']['run_key']==run_key, retired
        assert run_key in retired['reason'] and not retired['research_adjustment_ready'], retired
        assert len(retired['adjustment_retirement']['blocked_days'])==MAX_CONSECUTIVE_BLOCKED_RUNS, retired
        db.rollback()  # all fixtures disappear, production tables untouched
        db.execute('SET TRANSACTION READ ONLY')
        rows = db.execute(EQUITY_DAILY_CONTROL_STATUS_SQL).fetchall()
        actual = status_payload(rows, retired_dates=adjustment_retirement_details(db, rows))
        print(json.dumps({'isolated_postgresql_cases_passed': 6, 'actual_equity_readiness': actual}, ensure_ascii=False))
        db.rollback()


if __name__ == '__main__':
    main()

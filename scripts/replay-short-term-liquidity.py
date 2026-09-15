"""Read-only stored-market A/B replay. No collection, publication or DB writes."""
import argparse
from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))
import psycopg
from psycopg.rows import dict_row
from app.db_dsn import connection_params
from app.short_term_lanes.repository import load, verified_events
from app.short_term_lanes.rules import screen


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--date',required=True,type=date.fromisoformat)
    p.add_argument('--env-file',type=Path,default=Path('G:/StockPlatform/config/runtime.env'))
    args=p.parse_args()
    # Use only connection fields; never print private configuration or DSNs.
    env={}
    for line in args.env_file.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1)
            if key.strip() in {'PGHOST','PGPORT','PGDATABASE','PGUSER','PGPASSWORD'}:
                env[key.strip()]=value.strip().strip('"').strip("'")
    class ReadOnly:
        @contextmanager
        def transaction(self):
            with psycopg.connect(**connection_params(env),row_factory=dict_row,connect_timeout=10,
                                 options='-c default_transaction_read_only=on -c statement_timeout=30000') as c:
                yield c
    db=ReadOnly()
    rows,sessions=load(db,args.date)
    events=verified_events(db,args.date)
    # Historical common-input comparison. Disabling the new rerank reproduces
    # the old lane allocation, not a new trade/backtest signal.
    with patch('app.short_term_lanes.rules.rerank',lambda rows,**_: rows):
        before=screen(rows,sessions,str(args.date),events=events)
    after=screen(rows,sessions,str(args.date),events=events)
    def brief(lane):
        return [dict(symbol=r['symbol'],name=r['name'],amount_yi=round(r['metrics']['amount']/1e8,2),
                     turnover=r['metrics']['turnover']) for r in lane['selected']]
    print(json.dumps(dict(mode='stored_daily_readonly_no_publication',requested_date=str(args.date),
                          sessions=sessions,status=after['status'],coverage=after['coverage'],
                          strict_ohlc='not_fetched_no_claim_of_contraction_or_reclaim_coverage',
                          lanes=[dict(key=a['key'],matches=a['total_matches'],before=brief(b),after=brief(a))
                                 for b,a in zip(before['lanes'],after['lanes'])]),ensure_ascii=False,indent=2))
    if after['status'] != 'completed':
        raise SystemExit(2)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()

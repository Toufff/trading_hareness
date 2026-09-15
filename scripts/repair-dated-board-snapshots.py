"""Rebuild the three outage sessions from already verified dated stock snapshots."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
from app.db_dsn import connection_params
from app.longhu_board_close import aggregate_board


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    cfg=dict(l.split('=',1) for l in Path(r'G:\StockPlatform\config\runtime.env').read_text(encoding='utf-8-sig').splitlines() if '=' in l and not l.startswith('#'))
    results=[]
    with psycopg.connect(**connection_params(cfg),row_factory=dict_row) as c:
        for day in ('2026-09-07','2026-09-08','2026-09-09'):
            rows=c.execute("""SELECT raw->'screen_snapshot' AS row FROM quant.stock_money_flow_daily
                WHERE trading_date=%s AND source='longhuvip_main_net' AND raw ? 'screen_snapshot'""",(day,)).fetchall()
            if len(rows)<4900: raise RuntimeError(f'{day}: insufficient dated member coverage')
            groups=defaultdict(list)
            for r in rows: groups[str(r['row']['plate_id'])].append(r['row'])
            boards=[aggregate_board({'sector_key':key,'label':members[0].get('sector_label',key)},members,date.fromisoformat(day)) for key,members in groups.items()]
            existing=c.execute("""SELECT board_report_id FROM quant.intraday_board_reports
                WHERE source_status->>'repair_key'=%s""",('dated-member-repair:'+day,)).fetchone()
            if a.apply and not existing:
                c.execute("""INSERT INTO quant.intraday_board_reports(observed_at,status,source_status,summary,payload)
                    VALUES(%s,'completed',%s,%s,%s)""",(datetime.now(timezone.utc),
                    Json({'provider':'longhuvip_composite','trade_date':day,'data_as_of':day+'T15:00:00+08:00',
                          'repair_key':'dated-member-repair:'+day,'basis':'dated_member_aggregate','original_wrong_live_aggregates_not_reused':True}),
                    Json({}),Json({'status':'completed','items':boards,'source':'longhuvip_composite'})))
            results.append({'date':day,'stocks':len(rows),'boards':len(boards),'written':a.apply and not existing})
        if not a.apply: c.rollback()
    print(json.dumps(results))


if __name__=='__main__':main()

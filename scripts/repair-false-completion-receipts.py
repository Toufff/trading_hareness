"""Reopen only proven status contradictions; preserve the prior diagnostic."""
import argparse
import json
from pathlib import Path
import sys
import psycopg
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))
from app.db_dsn import connection_params


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');a=p.parse_args()
    cfg=dict(l.split('=',1) for l in Path(r'G:\StockPlatform\config\runtime.env').read_text(encoding='utf-8-sig').splitlines() if '=' in l and not l.startswith('#'))
    with psycopg.connect(**connection_params(cfg)) as c:
        rows=c.execute("""SELECT task_key,run_key,output_summary->>'status' FROM quant.automation_runs
            WHERE status='completed' AND output_summary->>'status' IN ('blocked','failed','partial')""").fetchall()
        if a.apply:
            c.execute("""UPDATE quant.automation_runs SET status=output_summary->>'status',updated_at=now(),
                output_summary=output_summary || '{"receipt_repaired":"2026-09-10","prior_receipt_status":"completed"}'::jsonb
                WHERE status='completed' AND output_summary->>'status' IN ('blocked','failed','partial')""")
        else: c.rollback()
    print(json.dumps({'applied':a.apply,'contradictions':len(rows),'runs':rows}))


if __name__=='__main__':main()

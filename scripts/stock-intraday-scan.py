"""Unified nine-strategy intraday scan, frozen replay and close comparison."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay')
    p.add_argument('--reconcile',help='Completed 15:00 run ID; compares earlier same-day scans without changing them')
    p.add_argument('--output-root',default='G:/StockPlatform/reports/intraday')
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    a=p.parse_args()
    if a.replay and a.reconcile:p.error('Choose replay or reconcile')
    from app.intraday_scan.runner import run,replay
    if a.replay:receipt=replay(a.replay,a.output_root)
    else:
        from dotenv import load_dotenv
        load_dotenv(a.env_file,override=True)
        from app.database import Database
        db=Database()
        try:
            if a.reconcile:
                from app.intraday_scan.reconcile import persist_day
                receipt=persist_day(db,a.reconcile)
            else:receipt=run(db,a.output_root)
        finally:db.close()
    print(json.dumps(receipt,ensure_ascii=False,default=str))

if __name__=='__main__':main()

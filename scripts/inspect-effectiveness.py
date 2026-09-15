"""Read real ledger and emit a concise diagnostic. --write publishes issue evidence only."""
import argparse,json,os,sys
from pathlib import Path
from datetime import date
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--date',required=True,type=date.fromisoformat)
    p.add_argument('--write',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env');a=p.parse_args()
    from dotenv import load_dotenv
    load_dotenv(a.env_file,override=True)
    from app.database import Database
    from app.effectiveness.service import build
    db=Database()
    try:result=build(db,a.date,write=a.write)
    finally:db.close()
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('status','as_of_date','row_count','finding_count','issue_ids','manual_rows')},ensure_ascii=False))
    print(json.dumps({'cohorts':len(result['groups']),'max_independent_dates':max((g['independent_sessions'] for g in result['groups']),default=0),
        'prospective_cohorts':sum(g['timing']=='prospective' for g in result['groups'])},ensure_ascii=False))


if __name__=='__main__':main()

"""Refresh follow-up evidence and governance routing without rescoring or pool edits."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True,type=date.fromisoformat)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--output',required=True)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    for line in Path(a.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.split('=',1)
            if key.startswith('PG') or key in {'QUANT_DATA_DIR','QUANT_GOVERNANCE_EVIDENCE_ROOT'}:os.environ[key]=value
    from app.database import Database
    from app.short_term_lanes.tracking_repository import refresh
    from app.strategy_governance.work_queue import prepare_queue
    from app.effectiveness.service import build
    db=Database()
    try:
        tracking=refresh(db,{},a.date,write=a.apply,import_history=False)
        effectiveness=build(db,a.date,write=a.apply)
        queue=prepare_queue(db) if a.apply else {'status':'not_mutated'}
        result={'as_of_date':str(a.date),'applied':a.apply,'followup':tracking,'effectiveness':effectiveness,'governance':queue,
                'pool_changed':False,'discipline_changed':False,'orders':False}
    finally:db.close()
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'tracking':tracking['status'],'rows':tracking.get('ledger_rows'),
        'effectiveness':effectiveness['status'],'governance':queue['status'],'pool_changed':False},ensure_ascii=False))


if __name__=='__main__':main()

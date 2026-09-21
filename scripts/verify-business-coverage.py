"""Read-only production business acceptance; no generation or external messages."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--output',required=True)
    a=p.parse_args()
    for line in Path(a.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.split('=',1)
            if key.startswith('PG'):os.environ[key]=value
    from app.database import Database
    from app.system_business_acceptance import collect
    db=Database()
    try:result=collect(db)
    finally:db.close()
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':[{k:c[k] for k in ('key','status')} for c in result['checks']]},ensure_ascii=False))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())

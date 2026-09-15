"""Prepare or run deterministic, isolated factor-profile engineering experiments."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','run'])
    p.add_argument('--input',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--item-id')
    p.add_argument('--candidate-profile',type=Path)
    p.add_argument('--allowed-strategy',action='append',default=[])
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    args=p.parse_args()
    for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.split('=',1);os.environ[key]=value
    from app.database import Database
    from app.strategy_governance.experiment_runner import prepare_context,run_experiment,_write_new
    from app.strategy_governance.rules import GovernanceError
    db=Database()
    try:
        if args.action=='prepare':
            if not args.candidate_profile:p.error('--candidate-profile required for prepare')
            profile=json.loads(args.candidate_profile.read_text(encoding='utf-8-sig'))
            result=prepare_context(db,args.input,profile,args.allowed_strategy)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            _write_new(args.output,result)
        else:
            if not args.item_id:p.error('--item-id required for run')
            result=run_experiment(db,args.item_id,args.input,args.output)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (GovernanceError,OSError,ValueError) as error:
        print(json.dumps({'status':'rejected','reason':str(error)},ensure_ascii=False))
        raise SystemExit(2)
    finally:
        db.close()


if __name__=='__main__':main()

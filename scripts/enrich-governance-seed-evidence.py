"""Attach actual frozen PV evidence, preserving original discovery revisions."""
import argparse,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'quant-service'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--old-release',type=Path,required=True)
    p.add_argument('--new-release',type=Path,required=True)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    args=p.parse_args();sys.stdout.reconfigure(encoding='utf-8')
    for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            k,v=line.split('=',1);os.environ[k]=v
    from app.database import Database
    from app.strategy_governance.seed_review_evidence import build_seed_packets
    from app.strategy_governance.repository import get_issue
    from app.strategy_governance.review_evidence import attach_review_packet,load_review_packet
    refs=build_seed_packets(args.directory,args.old_release,args.new_release)
    seeds=json.loads((args.directory/'seed-receipt.json').read_text(encoding='utf-8'));db=Database();out=[]
    try:
        for seed in seeds:
            item=get_issue(db,seed['id']);ref=refs[seed['change_id']]
            if item['state']=='discovered':
                item=attach_review_packet(db,item['id'],item['revision'],ref,{'id':'system:evidence-backfill','roles':['observer']})
                out.append({'change_id':seed['change_id'],'id':item['id'],'revision':item['revision'],
                    'evidence_status':load_review_packet(item)['status'],'reference':ref})
            else:
                out.append({'change_id':seed['change_id'],'state':item['state'],'status':'not_overwritten_after_review','reference':ref})
        path=args.directory/'seed-evidence-receipt.json'
        path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(out,ensure_ascii=False,indent=2))
    finally:db.close()


if __name__=='__main__':main()

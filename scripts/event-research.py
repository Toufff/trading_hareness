"""Collect/review news using the production DB; no secrets in artifacts."""
import argparse,json,sys
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
from dotenv import load_dotenv
load_dotenv('G:/StockPlatform/config/runtime.env',override=True)
from app.database import Database
from app.event_research.pipeline import run
from app.event_research.repository import documents
from app.event_research.report import sections

def main():
    p=argparse.ArgumentParser();p.add_argument('--no-fetch',action='store_true');p.add_argument('--review-file',type=Path)
    p.add_argument('--export-evidence',type=Path);p.add_argument('--output-dir',type=Path,default=Path('G:/StockPlatform/reports/events'))
    args=p.parse_args();db=Database()
    try:
        review=json.loads(args.review_file.read_text(encoding='utf-8-sig')) if args.review_file else None
        result=run(db,refresh=not args.no_fetch,review=review)
        args.output_dir.mkdir(parents=True,exist_ok=True)
        stem=args.output_dir/result['run_id']
        stem.with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        stem.with_suffix('.md').write_text('\n'.join(sections(result)),encoding='utf-8')
        if args.export_evidence:args.export_evidence.write_text(json.dumps(documents(db,datetime.fromisoformat(result['cutoff'])),ensure_ascii=False),encoding='utf-8')
        print(json.dumps({k:result[k] for k in ('run_id','cutoff','status','source_status','analysis','coverage')},ensure_ascii=False))
        if result['status'] not in ('analyzed','no_news') or result['analysis'].get('status')=='failed':
            raise SystemExit(2)
    finally:db.close()

if __name__=='__main__':main()

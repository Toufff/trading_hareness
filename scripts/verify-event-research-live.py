"""Explicit live acceptance: optional scan mutation, deployed readback, report parity."""
import argparse
import json
import os
import sys
from pathlib import Path
import requests
from dotenv import load_dotenv

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--date',required=True)
    p.add_argument('--run-scan',action='store_true')
    p.add_argument('--runtime',type=Path,default=Path('G:/StockPlatform/current'))
    args=p.parse_args()
    load_dotenv('G:/StockPlatform/config/runtime.env',override=True)
    sys.path.insert(0,str(args.runtime/'quant-service'))
    from app.short_term_lanes.reports import write_bundle
    session=requests.Session();session.trust_env=False
    def get(url):
        r=session.get(url,timeout=45);r.raise_for_status();return r.json()
    if args.run_scan:
        r=session.post('http://127.0.0.1:5681/api/v1/strategy/post-close/run',
            headers={'X-Quant-Write-Key':os.environ['QUANT_WRITE_API_KEY']},
            json={'as_of_date':args.date},timeout=240)
        r.raise_for_status()
        response=r.json()
        print(json.dumps({'scan':{k:response.get(k) for k in ('status','run_id','as_of_date')}},ensure_ascii=False),flush=True)
    owner=get('http://127.0.0.1:5681/api/v1/strategy/events/latest')
    adapter=get('http://127.0.0.1:5680/api/research/strategy/events/latest')
    scan=get('http://127.0.0.1:5681/api/v1/strategy/post-close/latest?as_of_date='+args.date)
    result=scan['run']['summary']['strategy_lanes'];news=result.get('event_research',{})
    bundle=result['report_bundle']
    if args.run_scan:write_bundle(Path('G:/StockPlatform/reports/short-term'),result,bundle)
    checks={
        'owner_adapter_same_run':owner['run_id']==adapter['run_id'],
        'owner_adapter_same_events':owner['events']==adapter['events'],
        'strategy_same_event_run':news.get('run_id')==owner['run_id'],
        'ten_reports':len(bundle['reports'])==10,
        'all_reports_contain_news':all('消息变化与方向影响' in x['markdown'] for x in bundle['reports']),
        'all_reports_same_news_run':all(owner['run_id'] in x['markdown'] for x in bundle['reports']),
        'source_receipt_present':bool(owner['source_status']),
        'bounded_provider_calls':all(h.get('physical_batch_limit',301)<=300 for h in owner['source_status']),
        'explicit_research_mode':bool(owner['analysis'].get('mode')),
        'semantic_analysis_completed':owner['analysis'].get('status')=='completed',
        'research_does_not_authorize_buy':owner['buy_authorized'] is False,
        'reviewed_evidence_real':all(set(e['evidence_ids'])<=set(owner['document_ids']) for e in owner['events']),
    }
    receipt={'passed':all(checks.values()),'checks':checks,'event_run':owner['run_id'],
        'strategy_run':scan['run']['id'] if 'id' in scan['run'] else scan['run'].get('run_id'),
        'event_count':len(owner['events']),'analysis':owner['analysis'],'coverage':owner['coverage']}
    path=Path('G:/StockPlatform/reports/events/live-acceptance.json')
    path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False),flush=True)
    return 0 if receipt['passed'] else 1

if __name__=='__main__':raise SystemExit(main())

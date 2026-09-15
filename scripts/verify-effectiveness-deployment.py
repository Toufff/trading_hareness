"""Actual scan -> owner -> adapter -> public -> reports, plus ranking invariance."""
import argparse,hashlib,json,sys
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'quant-service'))
TARGET=Path('G:/StockPlatform/data/research/effectiveness-acceptance')


def ranking(scan):
    return {l['key']:{k:[(r['symbol'],r.get('rank_score'),r.get('state')) for r in l.get(k,[])]
        for k in ('selected','observation_list','tracking_candidates')} for l in scan['lanes']}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser();p.add_argument('--baseline',action='store_true');p.add_argument('--run',action='store_true');a=p.parse_args()
    TARGET.mkdir(parents=True,exist_ok=True)
    session=requests.Session();session.trust_env=False
    path='/api/v1/strategy/post-close/latest';params={'as_of_date':'2026-09-11','view':'dashboard'}
    if a.baseline:
        r=session.get('http://127.0.0.1:5681'+path,params=params,timeout=30);r.raise_for_status()
        scan=r.json()['run']['summary']['strategy_lanes']
        (TARGET/'ranking-before.json').write_text(json.dumps(ranking(scan),ensure_ascii=False),encoding='utf-8')
        print('Baseline ranks frozen; no state mutation');return
    if a.run:
        from dotenv import dotenv_values
        env=dotenv_values('G:/StockPlatform/config/runtime.env')
        r=session.post('http://127.0.0.1:5681/api/v1/strategy/post-close/run',json={'as_of_date':'2026-09-11'},headers={'X-Quant-Write-Key':env['QUANT_WRITE_API_KEY']},timeout=240)
        r.raise_for_status();print(json.dumps({'stage':'real_scan','http':r.status_code}),flush=True)
    creds=json.loads(Path('C:/Users/brave/.stockbrain/dashboard-credentials.json').read_text())
    public=requests.Session();public.trust_env=False;public.auth=(creds['username'],creds['password'])
    public.cookies.set('stockbrain_access',creds['magic_cookie_token'],domain='stock.toufai.top')
    scans=[];receipts=[]
    for client,url in ((session,'http://127.0.0.1:5681'+path),(session,'http://127.0.0.1:5680/api/research/strategy/post-close/latest'),(public,'https://stock.toufai.top'+path)):
        r=client.get(url,params=params,timeout=45);r.raise_for_status();run=r.json()['run'];scan=run['summary']['strategy_lanes']
        scans.append(scan);receipts.append({'url':url,'run_id':run['run_id'],'http':r.status_code})
    first=scans[0];effect=first.get('effectiveness',{})
    from app.short_term_lanes.reports import write_bundle
    write_bundle(Path('G:/StockPlatform/reports/short-term'),first,first['report_bundle'])
    saved=json.loads(Path('G:/StockPlatform/reports/short-term/2026-09-11_short_term_lanes.json').read_text(encoding='utf-8'))
    before=json.loads((TARGET/'ranking-before.json').read_text(encoding='utf-8'))
    checks={'all_projections_equal':all(s==first for s in scans),'file_equals_api':saved==first,
        'effectiveness_completed':effect.get('status')=='completed','ten_reports_contain_feedback':len(first['report_bundle']['reports'])==10 and all('策略效果反馈' in r['markdown'] for r in first['report_bundle']['reports']),
        'production_rankings_unchanged':json.loads(json.dumps(ranking(first)))==before,
        'no_live_effect':effect.get('live_effect')=='none'}
    receipt={'passed':all(checks.values()),'checks':checks,'projections':receipts,'rows':effect.get('row_count'),
        'finding_count':effect.get('finding_count'),'profitability_validated':False}
    (TARGET/'deployment-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False))
    if not receipt['passed']:raise SystemExit(1)


if __name__=='__main__':main()

"""Real scan + owner/adapter/public/report readback for this work package only."""
import argparse
import hashlib,json,sys,time
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
TARGET=Path('G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance')


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',action='store_true');args=p.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    session=requests.Session();session.trust_env=False
    if args.run:
        env={line.split('=',1)[0]:line.split('=',1)[1] for line in Path('G:/StockPlatform/config/runtime.env').read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#')}
        response=session.post('http://127.0.0.1:5681/api/v1/strategy/post-close/run',json={'as_of_date':'2026-09-11'},headers={'X-Quant-Write-Key':env['QUANT_WRITE_API_KEY']},timeout=240)
        response.raise_for_status()
        print(json.dumps({'stage':'scan_request','http':response.status_code}),flush=True)
    results=[];bundles=[];governance=[];timings=[]
    credentials=json.loads(Path('C:/Users/brave/.stockbrain/dashboard-credentials.json').read_text())
    public=requests.Session();public.trust_env=False
    public.auth=(credentials['username'],credentials['password'])
    if credentials.get('magic_cookie_token'):public.cookies.set('stockbrain_access',credentials['magic_cookie_token'],domain='stock.toufai.top')
    for client,base,prefix in [(session,'http://127.0.0.1:5681','/api/v1/strategy'),(session,'http://127.0.0.1:5680','/api/research/strategy'),(public,'https://stock.toufai.top','/api/v1/strategy')]:
        print(json.dumps({'stage':'readback','endpoint':base}),flush=True)
        started=time.monotonic()
        response=client.get(base+prefix+'/post-close/latest',params={'as_of_date':'2026-09-11','view':'dashboard'},timeout=45);response.raise_for_status()
        timings.append({'endpoint':base,'resource':'post-close','seconds':round(time.monotonic()-started,3),'bytes':len(response.content)})
        print(json.dumps({'stage':'response',**timings[-1]}),flush=True)
        payload=response.json()
        assert payload.get('response_view')=='dashboard', 'Dashboard transport projection not deployed'
        run=payload['run'];result=run['summary']['strategy_lanes'];bundles.append(result)
        if args.run and len(bundles)==1:
            from app.short_term_lanes.reports import write_bundle
            write_bundle(Path('G:/StockPlatform/reports/short-term'),result,result['report_bundle'])
        results.append({'endpoint':base,'run_id':run['run_id'],'date':result['as_of_date'],'version':result['version'],'sha256':digest(result)})
        started=time.monotonic()
        response=client.get(base+prefix+'/governance',timeout=30);response.raise_for_status()
        timings.append({'endpoint':base,'resource':'governance','seconds':round(time.monotonic()-started,3),'bytes':len(response.content)})
        governance.append(response.json())
    result=bundles[0]
    from app.short_term_lanes.reports import write_bundle
    if args.run:write_bundle(Path('G:/StockPlatform/reports/short-term'),result,result['report_bundle'])
    saved=json.loads(Path('G:/StockPlatform/reports/short-term/2026-09-11_short_term_lanes.json').read_text(encoding='utf-8'))
    rows=[r for lane in result['lanes'] for r in lane['selected']+lane.get('caution_list',[])]
    archived=result.get('governance_input') or {}
    archive_path=Path(archived.get('input_path','__missing__'))
    checks={'all_projections_same':all(x==result for x in bundles),'file_same':saved==result,
        'new_version':result['version']=='short-term-lanes-price-volume-2026-09-11',
        'nine_strategies':len(result['lanes'])==9,'ten_reports':len(result['report_bundle']['reports'])==10,
        'all_displayed_have_pv':bool(rows) and all(r.get('price_volume',{}).get('version')=='price-volume-evidence-2026-09-11' for r in rows),
        'governance_same':governance[0]==governance[1]==governance[2],
        'governance_8_issues':len(governance[0]['items'])>=8,'no_live_factor_activation':governance[0]['active'] is None,
        'governance_inspection':result.get('governance_check',{}).get('status')=='completed',
        'same_run_input_archived':archived.get('status')=='ready' and archive_path.is_file()
            and hashlib.sha256(archive_path.read_bytes()).hexdigest()==archived.get('input_hash')}
    receipt={'passed':all(checks.values()),'checks':checks,'projections':results,'timings':timings,
        'history_enrichment':result.get('history_enrichment'),'governance_check':result.get('governance_check'),
        'scope':'本工作包真实扫描、治理读取与发布一致性；非收益验证，非券商持仓同步验收'}
    (TARGET/'deployment-readback.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False))
    if not receipt['passed']:raise SystemExit(1)


if __name__=='__main__':main()

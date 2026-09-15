"""Freeze same-source real inputs once and compare research versions read-only."""
import argparse
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--code-root',required=True,type=Path)
    p.add_argument('--directory',required=True,type=Path)
    p.add_argument('--date',required=True,type=date.fromisoformat)
    p.add_argument('--capture',action='store_true')
    args=p.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0,str(args.code_root/'quant-service'))
    from app.short_term_lanes.rules import screen, Settings
    from app.short_term_lanes.selection import project
    from app.short_term_lanes.reports import make_bundle,write_bundle
    target=args.directory.resolve()
    allowed=Path('G:/StockPlatform/data/research').resolve()
    if allowed not in target.parents: p.error('Evidence directory must be below G:/StockPlatform/data/research')
    target.mkdir(parents=True,exist_ok=True)
    path=target/'frozen-input.json'
    if args.capture:
        if path.exists(): p.error('Frozen input already exists; refusing overwrite')
        for line in Path('G:/StockPlatform/config/runtime.env').read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.startswith('#'):
                key,value=line.split('=',1); os.environ[key]=value
        import psycopg
        from psycopg.rows import dict_row
        from app.db_dsn import connection_params
        from app.short_term_lanes.repository import load,verified_events
        from app.short_term_lanes.reviews import load as load_reviews
        from app.short_term_lanes.candidate_history import fetch
        from app.short_term_lanes.advanced_strategies import prefilter_symbols
        class DB:
            @contextmanager
            def transaction(self):
                with psycopg.connect(**connection_params(),row_factory=dict_row,
                      options='-c default_transaction_read_only=on -c statement_timeout=30000') as c: yield c
        db=DB();rows,sessions=load(db,args.date);events=verified_events(db,args.date);reviews=load_reviews(db,args.date)
        for symbol,review in reviews.items():
            if not review.get('catalyst'):continue
            s=review['sources'][0];a=review.get('event_assessment') or {}
            events.setdefault(symbol,[]).append(dict(verified=True,benefit=review['catalyst'],url=s['url'],
                published_date=s['published_date'],available_at=a.get('available_at') or s.get('available_at') or s['published_date']+'T23:59:59+08:00',
                event_type=a.get('event_type','company_catalyst'),surprise=a.get('surprise','unknown'),
                priced_in=a.get('priced_in','unknown'),impact_direction=a.get('impact_direction','unknown')))
        pre=screen(rows,sessions,str(args.date),events=events,settings=Settings())
        queue=[]
        pools=[l['selected']+l.get('caution_list',[]) for l in pre['lanes']]
        for n in range(max(map(len,pools),default=0)):
            for pool in pools:
                if n<len(pool) and pool[n]['symbol'] not in queue:queue.append(pool[n]['symbol'])
        queue=list(dict.fromkeys(queue+prefilter_symbols(rows,sessions,limit=96)))[:96]
        print(json.dumps({'stage':'capture','rows':len(rows),'ohlc_requests':len(queue)}),flush=True)
        histories,health=fetch(queue,args.date)
        frozen=dict(date=str(args.date),rows=rows,sessions=sessions,events=events,reviews=reviews,
                    price_histories=histories,history_health=health)
        path.write_text(json.dumps(frozen,ensure_ascii=False,default=str),encoding='utf-8')
    frozen=json.loads(path.read_text(encoding='utf-8'))
    assert frozen['date']==str(args.date)
    result=screen(frozen['rows'],frozen['sessions'],str(args.date),events=frozen['events'],
                  price_histories=frozen['price_histories'],history_health=frozen['history_health'],settings=Settings())
    result['company_reviews']=list(frozen['reviews'].values())
    result.update(project(result,result['company_reviews']))
    result['report_bundle']=make_bundle(result)
    key='baseline' if args.capture else 'candidate'
    write_bundle(target/key,result,result['report_bundle'])
    checks={'same_date':result['as_of_date']==str(args.date),'completed':result['status']=='completed',
            'nine_lanes':len(result['lanes'])==9,'reports_ten':len(result['report_bundle']['reports'])==10,
            'no_buy_authorization':all(not r['buy_authorized'] for l in result['lanes'] for r in l.get('tracking_candidates',[]))}
    diff=[]
    if not args.capture:
        base=json.loads((target/'baseline'/f'{args.date}_short_term_lanes.json').read_text(encoding='utf-8'))
        for a,b in zip(base['lanes'],result['lanes']):
            checks[b['key']+':structured_pv']=all('price_volume' in r for r in b['selected']+b['caution_list'])
            checks[b['key']+':report_evidence']=not (b['selected']+b['caution_list']) or '量价核对' in next(x['markdown'] for x in result['report_bundle']['reports'] if x['key']==b['key'])
            diff.append(dict(lane=b['key'],label=b['label'],before_matches=a['total_matches'],after_matches=b['total_matches'],
                before=[r['name'] for r in a['selected']],after=[dict(name=r['name'],symbol=r['symbol'],evidence=r.get('price_volume')) for r in b['selected']],
                caution=[dict(name=r['name'],evidence=r.get('price_volume')) for r in b['caution_list']]))
    receipt=dict(kind=key,passed=all(checks.values()),checks=checks,input_hash=digest(frozen),
                 version=result['version'],history=frozen['history_health'],differences=diff,
                 scope='同一真实G盘输入与Longhu历史，研究回放；不是收益有效性或调度稳定性证明')
    (target/f'{key}-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k!='differences'},ensure_ascii=False))
    return 0 if receipt['passed'] else 1


if __name__=='__main__': raise SystemExit(main())

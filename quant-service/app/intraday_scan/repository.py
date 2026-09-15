"""One G-drive database. Intraday snapshots never overwrite settled daily bars."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from psycopg.types.json import Json
from .rules import digest

def seeds(database,day,cutoff):
    with database.transaction() as c:
        r=c.execute('''SELECT run_id,updated_at,summary->'strategy_lanes' AS scan
            FROM quant.post_close_strategy_runs WHERE as_of_date<%s AND updated_at<=%s
              AND summary ? 'strategy_lanes' ORDER BY as_of_date DESC,updated_at DESC LIMIT 1''',(day,cutoff)).fetchone()
        manual=c.execute('''SELECT evidence FROM quant.strategy_observation_origins
            WHERE signal_date BETWEEN %s AND %s AND evidence->>'source'='manual_recommendation'
              AND (evidence->>'available_at')::timestamptz<=%s
            ORDER BY signal_date,origin_id''',(day-timedelta(days=4),day,cutoff)).fetchall()
        previous_intraday=c.execute('''SELECT result FROM quant.intraday_strategy_scans
            WHERE cutoff<%s AND cutoff>=%s AND state='completed'
            ORDER BY cutoff DESC,created_at DESC LIMIT 1''',(cutoff,day-timedelta(days=4))).fetchone()
    result={};baseline={}
    if r:
        scan=r['scan'];baseline=dict(run_id=str(r['run_id']),date=scan['as_of_date'],version=scan.get('version'))
        for lane in scan.get('lanes',[]):
            displayed={x['symbol']:i+1 for i,x in enumerate(lane.get('observation_list',lane.get('selected',[])))}
            for item in lane.get('tracking_candidates',lane.get('selected',[])):
                m=item.get('metrics') or {};key=(item['symbol'],lane['key']);advanced=m.get('advanced') or {}
                reference=(advanced.get('reclaim_reference') if lane['key']=='reclaim' else
                           m.get('ma5') if lane['key'] in {'trend','pullback'} else m.get('prior_high'))
                support=(advanced.get('panic_low') if lane['key']=='reclaim' else
                         m.get('ma10') if lane['key'] in {'trend','pullback'} else m.get('recent_low'))
                result[key]=dict(symbol=item['symbol'],name=item['name'],lane=lane['key'],
                    origin_id=digest([str(r['run_id']),*key]),display_rank=displayed.get(item['symbol']),
                    reference=reference,support=support,original_reason=item.get('reason'),
                    original_confirmation=item.get('confirmation'),original_invalidation=item.get('invalidation'),
                    available_at=r['updated_at'].isoformat(),baseline=baseline,source='previous',
                    reference_basis='原策略结构参考；原文字买点不自动解析为已触发订单')
    if previous_intraday:
        for lane in previous_intraday['result']['lanes']:
            for item in lane['items']:
                key=(item['symbol'],item['lane'])
                if key not in result:
                    # Carry identity and ORIGINAL structural evidence only.
                    # Never leave a previous scan's VWAP gap/relative strength
                    # attached to a new quote whose minute check failed.
                    result[key]={k:v for k,v in item.items() if k in
                        {'symbol','name','lane','origin_id','display_rank','reference','support',
                         'original_reason','original_confirmation','original_invalidation','baseline',
                         'reference_basis','discovery_evidence','caveat','manual_recommended'}}
                    result[key].update(source='previous',original_reason=item.get('original_reason'),
                                       available_at=previous_intraday['result']['cutoff'])
    for record in manual:
        o=record['evidence']
        if not (o.get('effectiveness') or {}).get('selected'):continue
        key=(o['symbol'],o['lane']);existing=result.get(key,{})
        result[key]={**existing,**{k:o[k] for k in ('symbol','name','lane','origin_id','available_at')},
            'reference':existing.get('reference',o.get('reference_high')),
            'support':existing.get('support',o.get('reference_low')),'display_rank':0,
            'manual_recommended':True,'original_reason':o.get('manual',{}).get('reason',o.get('reason')),
            'original_confirmation':o.get('confirmation'),'source':'previous'}
    return list(result.values()),baseline

def previous_plans(database,cutoff):
    with database.transaction() as c:
        rows=c.execute('''SELECT result FROM quant.intraday_strategy_scans
            WHERE cutoff<%s AND state='completed' ORDER BY cutoff DESC,created_at DESC LIMIT 20''',(cutoff,)).fetchall()
    plans={}
    for r in rows:
        for lane in r['result']['lanes']:
            for x in lane['items']:
                if x.get('plan'):plans.setdefault(x['origin_id'],x['plan'])
    return plans

def begin(database,cutoff,version):
    run_id=str(uuid4())
    with database.transaction() as c:
        c.execute('''INSERT INTO quant.intraday_strategy_scans(run_id,cutoff,model_version,state,stage)
            VALUES(%s,%s,%s,'running','capture')''',(run_id,cutoff,version))
    return run_id

def save(database,run_id,data,result,stage='completed'):
    with database.transaction() as c:
        c.execute('''UPDATE quant.intraday_strategy_scans SET state='completed',stage=%s,
            input_hash=%s,input=%s,result=%s,updated_at=now() WHERE run_id=%s AND state='running' ''',
            (stage,digest(data),Json(data),Json(result),run_id))
        row=c.execute('SELECT input_hash,result FROM quant.intraday_strategy_scans WHERE run_id=%s',(run_id,)).fetchone()
    if row['input_hash']!=digest(data) or digest(row['result'])!=digest(result):raise ValueError('Database readback mismatch')
    return row

def fail(database,run_id,stage,exc):
    with database.transaction() as c:
        c.execute("UPDATE quant.intraday_strategy_scans SET state='failed',stage=%s,error=%s,updated_at=now() WHERE run_id=%s",
                  (stage,type(exc).__name__+': '+str(exc)[:500],run_id))

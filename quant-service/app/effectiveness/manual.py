"""Explicit human/assistant recommendation ledger with an unalterable registration time."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from ..short_term_lanes.tracking_rules import origins,digest
from ..short_term_lanes.tracking_repository import persist_origins


def records(scan,lane,symbols,author,reason,*,now=None):
    now=now or datetime.now(ZoneInfo('Asia/Shanghai'))
    if now.tzinfo is None:raise ValueError('Aware registration time required')
    day=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if not author.strip() or not reason.strip():raise ValueError('Author and contemporaneous reason required')
    chosen=set(symbols)
    source=next((l for l in scan['lanes'] if l['key']==lane),None)
    if not source:raise ValueError('Lane is absent from frozen scan')
    universe={r['symbol'] for r in source.get('tracking_candidates',[])}
    if not chosen or not chosen<universe:raise ValueError('Select a strict subset of frozen candidates; retain comparison group')
    if scan['as_of_date']>day:raise ValueError('Future source scan prohibited')
    from datetime import date
    if (date.fromisoformat(day)-date.fromisoformat(scan['as_of_date'])).days>3:raise ValueError('Source scan older than three calendar days; refresh first')
    snapshot=deepcopy(scan);snapshot['lanes']=[deepcopy(source)];snapshot['as_of_date']=day
    out=origins(snapshot,now.isoformat(),'live_scan')
    for r in out:
        r['profile']=digest([r['profile'],author])
        r['source']='manual_recommendation';r['effectiveness'].update(source_kind='manual',selected=r['symbol'] in chosen)
        r['manual']={'author':author,'reason':reason,'source_scan_date':scan['as_of_date'],'source_scan_hash':digest(scan)}
        # Stable across retries of the same recommendation on the same date;
        # inserts preserve first availability, never overwrite earlier opinions.
        r['origin_id']=digest({k:v for k,v in r.items() if k not in ('origin_id','available_at')})
    return out


def register(database,scan,lane,symbols,author,reason):
    out=records(scan,lane,symbols,author,reason)
    persist_origins(database,out)
    return {'status':'registered','selected':symbols,'comparison_universe':len(out),'live_effect':'none',
            'signal_date':out[0]['signal_date'],'available_at':out[0]['available_at']}

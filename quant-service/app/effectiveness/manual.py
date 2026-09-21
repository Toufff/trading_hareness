"""Explicit human/assistant recommendation ledger with an unalterable registration time."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from ..short_term_lanes.tracking_rules import origins,digest,identity_fields
from ..short_term_lanes.tracking_repository import persist_origins

SHANGHAI=ZoneInfo('Asia/Shanghai')
SESSION_CLOSE=time(15,0)


def session_closed_since(scan_date, now, sessions=None):
    """Whether any session settled between the frozen scan and the registration time.

    ``sessions`` are the exchange's open dates after the scan date; without a
    calendar every weekday is assumed open, which only errs toward refusing."""
    local=now.astimezone(SHANGHAI)
    today=local.date()
    start=date.fromisoformat(scan_date)
    if sessions is None:
        sessions=[start+timedelta(days=i) for i in range(1,(today-start).days+1) if (start+timedelta(days=i)).weekday()<5]
    for d in sessions:
        d=date.fromisoformat(str(d))
        if start<d<today or (d==today and local.time()>=SESSION_CLOSE):
            return True
    return False


def records(scan,lane,symbols,author,reason,*,now=None,sessions=None,attribution=None):
    now=now or datetime.now(SHANGHAI)
    if now.tzinfo is None:raise ValueError('Aware registration time required')
    day=now.astimezone(SHANGHAI).date().isoformat()
    if not author.strip() or not reason.strip():raise ValueError('Author and contemporaneous reason required')
    chosen=set(symbols)
    source=next((l for l in scan['lanes'] if l['key']==lane),None)
    if not source:raise ValueError('Lane is absent from frozen scan')
    universe={r['symbol'] for r in source.get('tracking_candidates',[])}
    if not chosen or not chosen<universe:raise ValueError('Select a strict subset of frozen candidates; retain comparison group')
    if scan['as_of_date']>day:raise ValueError('Future source scan prohibited')
    if (date.fromisoformat(day)-date.fromisoformat(scan['as_of_date'])).days>3:raise ValueError('Source scan older than three calendar days; refresh first')
    # The reference close is the scan date's close, so the signal date must be the
    # scan date: registering after midnight must not shift the first tracked
    # session forward. A scan whose next session already settled is stale; the
    # price moved without the recommendation, so it cannot be registered as-is.
    if session_closed_since(scan['as_of_date'],now,sessions):
        raise ValueError('A session has closed since the source scan; rescan before registering')
    snapshot=deepcopy(scan);snapshot['lanes']=[deepcopy(source)]
    out=origins(snapshot,now.isoformat(),'live_scan')
    for r in out:
        r['profile']=digest([r['profile'],author])
        r['source']='manual_recommendation';r['timing']='prospective'
        r['effectiveness'].update(source_kind='manual',selected=r['symbol'] in chosen)
        r['manual']={'author':author,'reason':reason,'source_scan_date':scan['as_of_date'],'source_scan_hash':digest(scan)}
        r['manual']['attribution'] = {'machine_rank':r['rank'], 'machine_display_rank':r['display_rank'],
            'selected':r['symbol'] in chosen, 'editorial':deepcopy((attribution or {}).get(r['symbol'])),
            'comparison_role':'selected' if r['symbol'] in chosen else 'nonselected_control',
            'outcome_at_registration':None}
        # Stable across retries of the same recommendation on the same scan;
        # the scan hash and attached research change with every report rebuild,
        # so they stay out of the identity. Inserts preserve first availability.
        identity=identity_fields(r)
        identity['manual']={k:v for k,v in r['manual'].items() if k!='source_scan_hash'}
        r['origin_id']=digest(identity)
    return out


def register(database,scan,lane,symbols,author,reason,*,attribution=None):
    now=datetime.now(SHANGHAI)
    with database.transaction() as c:
        sessions=[str(r['calendar_date']) for r in c.execute('''SELECT calendar_date FROM quant.market_trade_calendar
            WHERE exchange='SSE' AND is_open AND calendar_date>%s AND calendar_date<=%s ORDER BY calendar_date''',
            (scan['as_of_date'],now.date())).fetchall()]
    out=records(scan,lane,symbols,author,reason,now=now,sessions=sessions,attribution=attribution)
    persist_origins(database,out)
    return {'status':'registered','selected':symbols,'comparison_universe':len(out),'live_effect':'none',
            'signal_date':out[0]['signal_date'],'available_at':out[0]['available_at']}

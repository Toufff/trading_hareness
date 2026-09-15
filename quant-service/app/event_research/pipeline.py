"""Acquisition failure isolates news, never erases market or holdings advice."""
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import json
import logging
from . import repository
from .source import collect
from .contracts import VERSION,digest,eligible,unique_documents,timestamp
from .analysis import discover,model_review,research_sample,validate_review
from .schedule import delivery_state,scan_refresh_allowed
from .citations import SCHEME

def run(db,*,refresh=True,cutoff=None,review=None,collector=collect,analyzer=model_review):
    now=datetime.now(timezone.utc)
    # Cannot collect today's news and pretend it was known at a historical cutoff.
    if refresh and cutoff is not None and timestamp(cutoff)<now-timedelta(minutes=1):
        raise ValueError('Historical run cannot fetch current news')
    historical=cutoff is not None and timestamp(cutoff)<now-timedelta(minutes=1)
    if historical and review is not None:raise ValueError('Cannot backdate a new review')
    previous=repository.latest(db,timestamp(cutoff) if cutoff else now);health=[]
    if refresh:
        docs,status=collector();health.append(status);repository.store_documents(db,docs)
    elif previous:
        health=[{**h,'reused_capture':True} for h in previous.get('source_status',[])]
    cutoff=timestamp(cutoff) if cutoff else datetime.now(timezone.utc)
    docs=unique_documents([d for d in repository.documents(db,cutoff) if eligible(d,cutoff)])
    leads=discover(docs,repository.instruments(db))
    chosen=research_sample(docs,leads)
    input_hash=digest(json.dumps(docs,sort_keys=True,ensure_ascii=False))
    analysis={'status':'not_run'}; result_review=None
    try:
        if review is not None:
            result_review=validate_review(review,docs);analysis={'status':'completed','mode':'explicit_evidence_review'}
        elif previous and previous.get('input_hash')==input_hash and previous.get('analysis',{}).get('status')=='completed' and timestamp(previous['cutoff'])<=cutoff and (previous['analysis'].get('citation_scheme')==SCHEME or previous['analysis'].get('mode')=='explicit_evidence_review'):
            result_review={'summary':previous['summary'],'events':previous['events']};analysis={**previous['analysis'],'cache_hit':True}
        elif chosen and not historical:
            result_review,analysis=analyzer(chosen)
    except Exception as exc:
        logging.getLogger(__name__).exception('event_research_analysis_failed')
        # Persist a safe validation code: background CLI stderr may have no reader.
        known_errors={'Invalid semantic review','Too many reviewed events','Invalid category or surprise',
            'Unknown citation','Surprise requires prior-expectation evidence','Invalid importance',
            'Invalid symbols','Invalid symbol','Invalid direction','Missing business relation'}
        message=str(exc)
        safe_validation=isinstance(exc,ValueError) and (message in known_errors or
            message in {'Missing bounded reasoning: '+key for key in
                        ('fact','expectation','transmission','horizon','counterevidence','action','invalidate')})
        analysis={'status':'failed','error_type':type(exc).__name__,
                  'failure_code':message if safe_validation else type(exc).__name__}
        if hasattr(exc,'diagnostics'):
            analysis.update(exc.diagnostics)
        if isinstance(exc,TimeoutError):
            analysis['elapsed_seconds']=getattr(exc,'elapsed_seconds',None)
            analysis['progress']=getattr(exc,'progress',{})
    events=(result_review or {}).get('events',[])
    known_ids={d['document_id'] for d in docs}
    current_ids={e['event_id'] for e in events}
    retained=[{**e,'change':'retained'} for e in (previous or {}).get('events',[])
        if e['event_id'] not in current_ids and set(e['evidence_ids'])<=known_ids]
    events+=retained
    if retained and not result_review:analysis={**analysis,'retained_reviews':len(retained)}
    prior={e['event_id'] for e in (previous or {}).get('events',[])}
    for e in events:e['change']='retained' if e['event_id'] in prior else 'new'
    status=('failed' if health and all(h['status']=='failed' for h in health) else
        'no_news' if not docs else 'analyzed' if analysis['status']=='completed' else 'leads_only')
    result=dict(version=VERSION,run_id=str(uuid4()),cutoff=cutoff.isoformat(),status=status,
        input_hash=input_hash,document_ids=[d['document_id'] for d in docs],source_status=health,analysis=analysis,
        summary=(result_review or {}).get('summary','已获取消息线索，尚无通过引用校验的语义结论；不能据此声称没有利好。'),
        events=events,leads=leads[:100],coverage=dict(documents=len(docs),routed=len(leads),review_input=len(chosen),
        reviewed=len({i for e in events for i in e['evidence_ids']}),full_market_news=False),
        live_effect='research_only_no_weight_change',buy_authorized=False,
        published_at=datetime.now(timezone.utc).isoformat())
    repository.save(db,result)
    return result

def context(db,cutoff,*,refresh=False):
    """30-minute cache; new failed refresh remains visible, not old success."""
    cutoff=timestamp(cutoff)
    try:
        value=repository.latest(db,cutoff)
        if refresh and scan_refresh_allowed(cutoff) and (not value or cutoff-timestamp(value['cutoff'])>timedelta(minutes=30)):
            return run(db)
        if value:
            age=(cutoff-timestamp(value['cutoff'])).total_seconds()
            return {**value,**delivery_state(value,cutoff),'age_seconds':round(age)}
        return {'status':'absent','events':[],'leads':[],'summary':'本时点没有可用消息研究快照，不能判断为没有事件。'}
    except Exception as exc:
        logging.getLogger(__name__).exception('event_research_context_failed')
        return {'status':'failed','error_type':type(exc).__name__,'events':[],'leads':[],
            'summary':'消息链读取或刷新失败；量价扫描继续独立运行。'}

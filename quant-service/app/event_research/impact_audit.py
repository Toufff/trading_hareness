"""Content changes and consumption receipts without fabricating price impacts."""
from datetime import timedelta
import json
from .contracts import digest, timestamp


def event_version(event):
    return digest(json.dumps({k:v for k,v in event.items() if k not in
        {'change','content_version','evidence_timing','impact_audit'}},sort_keys=True,ensure_ascii=False))


def annotate(events, previous, documents, cutoff):
    prior={e['event_id']:e for e in previous}
    docs={d['document_id']:d for d in documents}
    current=[]
    for event in events:
        item=dict(event); old=prior.get(item['event_id']); version=event_version(item)
        item['content_version']=version
        item['change']='new' if not old else 'retained' if event_version(old)==version else 'updated'
        evidence=[docs[i] for i in item.get('evidence_ids',[]) if i in docs]
        item['evidence_timing']=[{k:d.get(k) for k in ('document_id','source','published_at','first_seen_at','available_at','provenance')} for d in evidence]
        expiry=min((timestamp(d['published_at'])+timedelta(days=7) for d in evidence),default=None)
        item['impact_audit']={'expires_at':expiry.isoformat() if expiry else None,
            'recheck_symbols':[s['symbol'] for s in item.get('symbols',[]) if s.get('symbol')],
            'surprise':item.get('surprise','unknown'), 'priced_in':'not_verified',
            'transmission':item.get('transmission'), 'suggested_attention':item.get('action'),
            'applied_to_strategy_weights':False, 'buy_authorized':False}
        current.append(item)
    ids={e['event_id'] for e in current}
    retired=[{'event_id':e['event_id'],'reason':'no_longer_supported_by_current_eligible_documents',
              'recheck_symbols':[s['symbol'] for s in e.get('symbols',[]) if s.get('symbol')]} for e in previous if e['event_id'] not in ids]
    return current, retired


def consumption(scan, context):
    symbols={r['symbol'] for lane in scan.get('lanes',[]) for r in lane.get('selected',[])+lane.get('caution_list',[])}
    return {'news_run_id':context.get('run_id'),'news_cutoff':context.get('cutoff'),
        'records':[{'event_id':e['event_id'],'content_version':e.get('content_version'),
                    'matched_symbols':sorted(symbols & {s['symbol'] for s in e.get('symbols',[]) if s.get('symbol')}),
                    'usage':'research_context_only_no_implicit_score_change','change':e.get('change')} for e in context.get('events',[])],
        'retired_events':context.get('retired_events',[]),
        'note':'此回执证明哪些候选关联了消息，不把关联冒充已改变评分或买入建议。'}


def at_cutoff(context, cutoff):
    """Expire cached evidence even when no new model refresh is scheduled."""
    events=[]; retired=list(context.get('retired_events',[]))
    retired_ids={e['event_id'] for e in retired}
    for event in context.get('events',[]):
        audit=event.get('impact_audit') or {}
        expiry=audit.get('expires_at')
        if expiry and timestamp(expiry)<timestamp(cutoff):
            if event['event_id'] not in retired_ids:
                retired.append({'event_id':event['event_id'],'reason':'evidence_expired_at_read_cutoff',
                                'recheck_symbols':audit.get('recheck_symbols',[])})
        else:
            events.append(event)
    return {**context,'events':events,'retired_events':retired}

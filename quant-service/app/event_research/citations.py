"""Request-local opaque references; original evidence hashes remain canonical."""
from copy import deepcopy

SCHEME = 'short_refs_v1'


def encode_documents(docs):
    if len({d['document_id'] for d in docs}) != len(docs):
        raise ValueError('Duplicate input evidence IDs')
    mapping={f'D{index:03d}':d['document_id'] for index,d in enumerate(docs,1)}
    wire=[dict(document_id=alias, **{k:d[k] for k in ('title','body','source','published_at')})
          for alias,d in zip(mapping,docs)]
    return wire,mapping


def invalid_events(review,mapping):
    if not isinstance(review,dict) or not isinstance(review.get('events'),list):
        raise ValueError('Invalid semantic review')
    invalid=[]
    for index,event in enumerate(review['events']):
        if not isinstance(event,dict):raise ValueError('Invalid semantic event')
        ids=event.get('evidence_ids')
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in mapping for i in ids):
            invalid.append(index)
    return invalid


def resolve_review(review,mapping):
    if invalid_events(review,mapping):raise ValueError('Unknown citation alias')
    result=deepcopy(review)
    for event in result['events']:
        event['evidence_ids']=[mapping[i] for i in event['evidence_ids']]
    return result


def apply_repairs(review,repair,indices,mapping):
    corrections=repair.get('corrections') if isinstance(repair,dict) else None
    if not isinstance(corrections,list) or len(corrections)!=len(indices):
        raise ValueError('Incomplete citation repair')
    result=deepcopy(review);seen=set()
    for correction in corrections:
        if not isinstance(correction,dict) or set(correction)!={'event_index','evidence_ids'}:
            raise ValueError('Citation repair may only change references')
        index=correction['event_index']
        if type(index) is not int or index not in indices or index in seen:
            raise ValueError('Unexpected citation repair index')
        result['events'][index]['evidence_ids']=correction['evidence_ids'];seen.add(index)
    if invalid_events(result,mapping):raise ValueError('Unresolved citation repair')
    return result

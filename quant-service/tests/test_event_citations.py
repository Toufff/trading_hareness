from copy import deepcopy
import pytest
from test_event_research import document, review
from app.event_research.citations import encode_documents, resolve_review, invalid_events, apply_repairs


def sample():
    docs=[document(),document(CID='456',Content='另外的真实证据')]
    wire,mapping=encode_documents(docs)
    r=review(docs[0]);r['events'][0]['evidence_ids']=['D001']
    return docs,wire,mapping,r


def test_wire_has_short_ids_and_persistence_has_exact_original_ids():
    docs,wire,mapping,r=sample()
    assert [d['document_id'] for d in wire]==['D001','D002']
    assert docs[0]['document_id'] not in str(wire)
    resolved=resolve_review(r,mapping)
    assert resolved['events'][0]['evidence_ids']==[docs[0]['document_id']]
    assert r['events'][0]['evidence_ids']==['D001']


@pytest.mark.parametrize('ids',[['D999'],['D01'],['d001'],[], 'D001', [None]])
def test_never_fuzzy_matches_or_drops_invalid_citations(ids):
    _,_,mapping,r=sample();r['events'][0]['evidence_ids']=ids
    assert invalid_events(r,mapping)==[0]
    with pytest.raises(ValueError):resolve_review(r,mapping)


def test_repairs_only_requested_citations_and_preserves_all_reasoning():
    _,_,mapping,r=sample();r['events'].append(deepcopy(r['events'][0]))
    r['events'][1]['evidence_ids']=['bad'];before=deepcopy(r)
    fixed=apply_repairs(r,{'corrections':[{'event_index':1,'evidence_ids':['D002']}]},[1],mapping)
    assert fixed['events'][0]==before['events'][0]
    assert fixed['events'][1]['fact']==before['events'][1]['fact']
    assert fixed['events'][1]['evidence_ids']==['D002']
    assert r==before
    for correction in ({'corrections':[]},{'corrections':[{'event_index':0,'evidence_ids':['D001']}]},
                       {'corrections':[{'event_index':1,'evidence_ids':['D900']}]},
                       {'corrections':[{'event_index':1,'evidence_ids':['D002'],'fact':'changed'}]}):
        with pytest.raises(ValueError):apply_repairs(r,correction,[1],mapping)

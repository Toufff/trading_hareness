from copy import deepcopy
import pytest
from test_event_research import document,review
from app.event_research import analysis,model_config
from app.event_research.model_client import ModelReviewFailure


def setup(monkeypatch, replies):
    monkeypatch.setattr(model_config,'settings',lambda:('https://example.invalid','secret','test',{}))
    calls=[]
    def complete(messages,connection,deadline):
        calls.append((messages,deadline))
        item=replies.pop(0)
        if isinstance(item,Exception):raise item
        return deepcopy(item),{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120},'test'
    monkeypatch.setattr(analysis,'completion',complete)
    return calls


def test_success_uses_short_ids_but_returns_canonical_evidence(monkeypatch):
    d=document();r=review(d);r['events'][0]['evidence_ids']=['D001']
    calls=setup(monkeypatch,[r]);result,meta=analysis.model_review([d])
    assert d['document_id'] not in calls[0][0][1]['content']
    assert result['events'][0]['sources'][0]['document_id']==d['document_id']
    assert meta['citation_scheme']=='short_refs_v1' and meta['repair_count']==0
    assert meta['input_document_ids']==[d['document_id']]


def test_one_targeted_repair_shares_deadline_and_usage(monkeypatch):
    d=document();r=review(d);r['events'][0]['evidence_ids']=['D01']
    calls=setup(monkeypatch,[r,{'corrections':[{'event_index':0,'evidence_ids':['D001']}]}])
    result,meta=analysis.model_review([d])
    assert len(calls)==2 and calls[0][1]==calls[1][1]
    assert result['events'][0]['fact']==r['events'][0]['fact']
    assert meta['repair_count']==1 and meta['usage']['total_tokens']==240
    assert meta['repaired_event_indices']==[0]


def test_bad_repair_cannot_be_success_and_is_not_retried_forever(monkeypatch):
    d=document();r=review(d);r['events'][0]['evidence_ids']=['D99']
    calls=setup(monkeypatch,[r,{'corrections':[{'event_index':0,'evidence_ids':[]}]}])
    with pytest.raises(ModelReviewFailure) as err:analysis.model_review([d])
    assert len(calls)==2
    assert err.value.diagnostics['failure_stage']=='citation_repair'
    assert err.value.diagnostics['failure_code']=='invalid_citations'
    assert len(err.value.diagnostics['attempts'])==2


def test_missing_reasoning_is_not_repaired_by_changing_references(monkeypatch):
    d=document();r=review(d);r['events'][0]['evidence_ids']=['D001'];del r['events'][0]['action']
    calls=setup(monkeypatch,[r])
    with pytest.raises(ModelReviewFailure) as err:analysis.model_review([d])
    assert len(calls)==1 and err.value.diagnostics['failure_stage']=='content_validation'


def test_connection_and_generation_failure_keep_specific_diagnostics(monkeypatch):
    setup(monkeypatch,[ModelReviewFailure('generation','total_deadline_exceeded',progress={'received_chars':0})])
    with pytest.raises(ModelReviewFailure) as err:analysis.model_review([document()])
    assert err.value.diagnostics['failure_stage']=='generation'
    assert err.value.diagnostics['progress']['received_chars']==0

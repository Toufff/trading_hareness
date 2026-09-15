from datetime import datetime, timezone, timedelta
from copy import deepcopy
import pytest
from app.event_research.contracts import normalize, eligible, unique_documents
from app.event_research.analysis import discover, validate_review
from app.event_research.report import sections

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)

def document(**kw):
    raw = dict(CID='123', Time=str(int(NOW.timestamp())-60), Title='天融信公告',
               Content='天融信发布网络安全产品。', Stocks=[['002212','天融信']], Source='财联社', PushUrl='')
    raw.update(kw)
    return normalize(raw, NOW)

def review(d):
    return dict(summary='产品发布，尚无新增订单证据。', events=[dict(
        evidence_ids=[d['document_id']], category='industry', fact='发布产品',
        expectation='unknown', surprise='unknown', transmission='先看需求能否形成收入。',
        horizon='未来数周', counterevidence='没有合同金额', action='观察板块与成交反馈',
        invalidate='产品没有商业化进展', importance=2,
        symbols=[dict(symbol='002212.SZ', name='天融信', relation='直接提及，受益未证实', direction='uncertain')])])

def test_document_times_and_reposts():
    d=document(); assert eligible(d,NOW)
    assert not eligible(d,NOW-timedelta(minutes=2))
    assert not eligible(document(Time=str(int(NOW.timestamp())+60)),NOW)
    old=document(Time=str(int((NOW-timedelta(days=20)).timestamp())))
    assert not eligible(old,NOW)
    assert len(unique_documents([d,document(CID='456')]))==1
    assert document(Content='更新：订单已取消')['document_id'] != d['document_id']

def test_unknown_is_not_positive_and_entities_not_holdings():
    d=document(); out=discover([d], [{'symbol':'002212.SZ','name':'天融信'}])
    assert out[0]['symbols'][0]['symbol']=='002212.SZ'
    assert out[0]['status']=='lead_only'
    assert out[0]['buy_authorized'] is False
    assert validate_review(review(d),[d])['events'][0]['surprise']=='unknown'

def test_reject_invented_citation_symbol_and_expectation():
    d=document(); r=review(d)
    for key,value in [('evidence_ids',['invented']),('surprise','positive')]:
        bad=deepcopy(r);bad['events'][0][key]=value
        with pytest.raises(ValueError): validate_review(bad,[d])
    bad=deepcopy(r);bad['events'][0]['symbols'][0]['symbol']='123'
    with pytest.raises(ValueError): validate_review(bad,[d])

def test_negative_surprise_despite_positive_headline():
    d=document(Title='利润增长20%',Content='利润增长20%，此前一致预期增长40%。')
    r=review(d);r['events'][0].update(expectation='原文载明此前预期增长40%',surprise='negative')
    assert validate_review(r,[d])['events'][0]['surprise']=='negative'

def test_failed_empty_and_unconfigured_are_different():
    failed='\n'.join(sections({'status':'failed','source_status':[{'status':'failed'}],'events':[],'leads':[]}))
    empty='\n'.join(sections({'status':'no_news','events':[],'leads':[]}))
    assert failed!=empty
    assert '失败' in failed and '没有返回' in empty

def test_markup_and_unsafe_urls():
    d=document(Content='<script>steal()</script><p>订单</p>',PushUrl='javascript:alert(1)')
    assert '<script>' not in d['body'] and d['url']==''

def test_real_wrapper_is_used_and_physical_pages_bounded(monkeypatch):
    from app.event_research import source
    from dataclasses import dataclass
    @dataclass
    class Config:
        retries:int=1
        timeout_seconds:float=1
    calls=[]
    def execute(**kw):
        calls.append(kw['params'])
        return {'calls':1,'pages':[{'payload':{'errcode':'0','List':[]}}]}
    monkeypatch.setattr(source,'execute',execute)
    docs,health=source.collect(config=Config(),session=object())
    assert not docs and health['status']=='ok' and health['exhausted']
    assert calls[0]['st']==300 and calls[0]['c']=='PCNewsFlash'
    with pytest.raises(ValueError):source.collect(page_size=301)

def test_pipeline_failure_is_persisted_without_invented_success(monkeypatch):
    from app.event_research import pipeline
    stored=[]
    monkeypatch.setattr(pipeline.repository,'latest',lambda *a:None)
    monkeypatch.setattr(pipeline.repository,'store_documents',lambda *a:None)
    monkeypatch.setattr(pipeline.repository,'documents',lambda *a:[])
    monkeypatch.setattr(pipeline.repository,'instruments',lambda *a:[])
    monkeypatch.setattr(pipeline.repository,'save',lambda db,r:stored.append(r))
    result=pipeline.run(object(),collector=lambda:([],{'status':'failed'}))
    assert result['status']=='failed' and stored[0]['status']=='failed'
    with pytest.raises(ValueError):pipeline.run(object(),cutoff=NOW-timedelta(days=40))

def test_reports_all_contain_same_event_run():
    from test_short_term_lane_reports import example
    from app.short_term_lanes.reports import make_bundle
    result=example();result['event_research']={'status':'leads_only','run_id':'same-event-run','events':[],'leads':[]}
    reports=make_bundle(result)['reports']
    assert all('same-event-run' in r['markdown'] for r in reports)
    assert all(next(line for line in r['markdown'].splitlines() if line.startswith('## '))=='## 本次结论' for r in reports)


def test_model_validation_diagnostics_persist_and_report_without_success(monkeypatch):
    from app.event_research import pipeline
    from app.event_research.model_client import ModelReviewFailure
    d=document();stored=[]
    monkeypatch.setattr(pipeline.repository,'latest',lambda *a:None)
    monkeypatch.setattr(pipeline.repository,'store_documents',lambda *a:None)
    monkeypatch.setattr(pipeline.repository,'documents',lambda *a:[d])
    monkeypatch.setattr(pipeline.repository,'instruments',lambda *a:[])
    monkeypatch.setattr(pipeline.repository,'save',lambda db,r:stored.append(r))
    monkeypatch.setattr(pipeline,'eligible',lambda *a:True)
    def fail(*a):raise ModelReviewFailure('citation_repair','invalid_citations',invalid_event_indices=[0])
    result=pipeline.run(object(),collector=lambda:([d],{'status':'ok'}),analyzer=fail)
    assert result['status']=='leads_only'
    assert stored[0]['analysis']['failure_stage']=='citation_repair'
    assert 'invalid_citations' in '\n'.join(sections(result))

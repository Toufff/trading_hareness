from copy import deepcopy

from app.event_research.analysis import normalize_model_symbols, normalize_model_metadata, validate_review


def test_overseas_or_unresolved_symbol_does_not_discard_valid_news_fact():
    event = dict(category='macro', fact='海外模型公司发布产品', expectation='unknown', surprise='unknown',
        transmission='影响A股应用成本预期，具体受益待原文证据', horizon='短期', counterevidence='尚无收入证据',
        action='观察', invalidate='消息不兑现', importance=2, evidence_ids=['a'],
        symbols=[dict(symbol='MSFT',name='微软',direction='positive',relation='海外公司'),
                 dict(symbol='002212.SZ',name='天融信',direction='uncertain',relation='假设待确认'),
                 dict(symbol='行业',name='行业')])
    review=dict(summary='保留事实，隔离非法A股关联',events=[event])
    before=deepcopy(event)
    rejected=normalize_model_symbols(review)
    assert len(rejected)==2
    assert event['fact']==before['fact'] and event['transmission']==before['transmission']
    assert [s['symbol'] for s in event['symbols']]==['002212.SZ']
    doc=dict(document_id='a',source='test',url='https://example.com',published_at='2026-09-14',
             available_at='2026-09-14',provenance='test')
    assert validate_review(review,[doc])['events'][0]['buy_authorized'] is False


def test_no_code_is_invented_from_company_name():
    data=dict(events=[dict(symbols=[dict(name='天融信',symbol='002212')])])
    assert normalize_model_symbols(data)
    assert data['events'][0]['symbols']==[]


def test_safe_validation_reason_is_persisted(monkeypatch):
    from datetime import datetime, timezone
    from app.event_research import pipeline
    from app.event_research.contracts import normalize
    now=datetime.now(timezone.utc)
    doc=normalize(dict(CID='x',Time=str(int(now.timestamp())-1),Title='央行消息',Content='央行公告'),now)
    monkeypatch.setattr(pipeline.repository,'latest',lambda *a:None)
    monkeypatch.setattr(pipeline.repository,'documents',lambda *a:[doc])
    monkeypatch.setattr(pipeline.repository,'instruments',lambda *a:[])
    monkeypatch.setattr(pipeline.repository,'save',lambda *a:None)
    def fail(_):raise ValueError('Invalid symbol')
    result=pipeline.run(object(),refresh=False,analyzer=fail)
    assert result['analysis']['failure_code']=='Invalid symbol'
    assert result['analysis']['status']=='failed'


def test_importance_protocol_is_lossless_or_conservative_never_increases_priority():
    values=['3',2.0,99,'high',None,True,3]
    result={'events':[{'importance':v,'fact':'unchanged','evidence_ids':['exact-id']} for v in values]}
    adjustments=normalize_model_metadata(result)
    assert [e['importance'] for e in result['events']]==[3,2,1,1,1,1,3]
    assert len(adjustments)==6
    assert all(e['fact']=='unchanged' and e['evidence_ids']==['exact-id'] for e in result['events'])

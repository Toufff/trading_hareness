from app.event_research.impact_audit import annotate, consumption


def test_correction_is_not_silently_labeled_retained():
    doc=dict(document_id='d', published_at='2026-09-21T10:00:00+08:00', available_at='2026-09-21T10:01:00+08:00')
    original=dict(event_id='e',fact='old',evidence_ids=['d'],symbols=[{'symbol':'600664.SH'}])
    first,_=annotate([original],[],[doc],doc['available_at'])
    same,_=annotate([original],first,[doc],doc['available_at'])
    assert same[0]['change']=='retained'
    changed,_=annotate([{**original,'fact':'corrected'}],first,[doc],doc['available_at'])
    assert changed[0]['change']=='updated'
    assert changed[0]['impact_audit']['expires_at']
    _,retired=annotate([],first,[],doc['available_at'])
    assert retired[0]['recheck_symbols']==['600664.SH']


def test_linkage_receipt_is_not_score_change_claim():
    r=consumption({'lanes':[{'selected':[{'symbol':'600664.SH'}]}]},
        {'run_id':'n','events':[{'event_id':'e','symbols':[{'symbol':'600664.SH'}]}]})
    assert r['records'][0]['matched_symbols']==['600664.SH']
    assert r['records'][0]['usage']=='research_context_only_no_implicit_score_change'


def test_same_provider_correction_supersedes_old_fact_without_deleting_document():
    from app.event_research.contracts import unique_documents
    old=dict(provider='longhu',provider_id='1',content_hash='old',available_at='2026-09-21T01:00:00Z')
    new={**old,'content_hash':'corrected','available_at':'2026-09-21T02:00:00Z'}
    assert unique_documents([new,old])==[new]
    assert old['content_hash']=='old'


def test_cached_events_expire_without_waiting_for_another_model_call():
    from app.event_research.impact_audit import at_cutoff
    old={'events':[{'event_id':'e','impact_audit':{'expires_at':'2026-09-21T01:00:00Z','recheck_symbols':['600664.SH']}}]}
    fresh=at_cutoff(old,'2026-09-22T01:00:00Z')
    assert fresh['events']==[] and fresh['retired_events'][0]['recheck_symbols']==['600664.SH']
    assert old['events']

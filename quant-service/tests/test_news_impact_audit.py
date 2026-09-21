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

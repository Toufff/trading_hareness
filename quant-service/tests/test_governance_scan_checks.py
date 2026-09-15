from copy import deepcopy
from app.short_term_lanes.governance_checks import findings


def test_inspection_is_atomic_and_does_not_mutate_scan():
    r={'version':'v1','as_of_date':'2026-09-11','status':'completed','lanes':[
       {'key':'trend','label':'趋势','selected':[{'symbol':'a'}],'caution_list':[]},
       {'key':'pullback','label':'回踩','selected':[{'symbol':'b'}],'caution_list':[]}]}
    before=deepcopy(r);result=findings(r)
    assert len(result)==2 and r==before
    assert len({x['dedupe_key'] for x in result})==2
    r['as_of_date']='2026-09-14'
    assert [x['dedupe_key'] for x in findings(r)]==[x['dedupe_key'] for x in result]


def test_unknown_is_not_false_pass_or_automatic_rewrite():
    r={'version':'v1','status':'completed','lanes':[{'key':'trend','label':'趋势',
       'selected':[{'symbol':'a','state':'watch','price_volume':{'status':'quality_unverified'}}]}]}
    assert findings(r)==[]

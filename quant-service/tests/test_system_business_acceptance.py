from app.system_business_acceptance import assess


def test_read_failures_and_two_of_five_never_green():
    r=assess({'discipline':{'coverage':{'holdings':{'expected':5,'covered':2}},'gaps':True},
              'errors':[{'section':'news','error_type':'TimeoutError'}]})
    assert r['status']=='partial'
    assert next(c for c in r['checks'] if c['key']=='discipline')['status']=='attention'
    assert next(c for c in r['checks'] if c['key']=='news_read')['status']=='attention'
    assert r['profitability_verified'] is False


def test_routed_but_stalled_queue_is_not_healthy():
    for values in ({'unrouted':0,'stale_pre_experiment':11}, {'unrouted':0,'blocked':2}, {}):
        report=assess({'governance':values})
        assert next(c for c in report['checks'] if c['key']=='governance')['status']=='attention'


def test_one_fresh_equity_cannot_represent_full_market():
    report=assess({'expected_date':'2026-09-21','market':{'date':'2026-09-21','stocks':1,'baseline_stocks':5000}})
    assert next(c for c in report['checks'] if c['key']=='equities')['status']=='attention'

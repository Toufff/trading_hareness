from app.short_term_lanes.tracking_rules import origins, evaluate


def sample():
    return dict(as_of_date='2026-09-10', version='v1', settings={},
        lanes=[dict(key='accumulation', label='潜伏', selected=[dict(symbol='001232.SZ',name='嘉立创',
        metrics={'close':100,'prior_high5':102,'low5':95}, confirmation='收回102并承接',invalidation='跌破95且无法修复')],caution_list=[])])


def test_origin_idempotent_and_conditions_frozen():
    a=origins(sample(), '2026-09-10T16:00:00+08:00','published')
    b=origins(sample(), '2026-09-11T16:00:00+08:00','published')
    assert a[0]['origin_id']==b[0]['origin_id']
    assert a[0]['confirmation']=='收回102并承接'
    r=sample();r['lanes'][0]['selected'][0]['confirmation']='different'
    assert origins(r,'2026-09-10T16:00:00+08:00','published')[0]['origin_id']!=a[0]['origin_id']


def test_origin_identity_ignores_attached_research_and_import_source():
    a=origins(sample(), '2026-09-10T16:00:00+08:00','live_scan')[0]
    r=sample();r['company_reviews']=[dict(symbol='001232.SZ',conclusion='研究后补')]
    b=origins(r,'2026-09-10T17:00:00+08:00','historical_run:abc')[0]
    assert b['company_review'] and a['company_review'] is None
    assert a['origin_id']==b['origin_id']
    assert (a['timing'],b['timing'])==('prospective','reconstructed')


def test_collapse_prefers_prospective_then_earliest_row():
    from app.short_term_lanes.tracking_repository import collapse
    base=dict(symbol='A',signal_date='2026-09-16',lane='expansion',display_rank=1,rank=1)
    rows=[dict(base,origin_id='2',timing='reconstructed',source='historical_run:x',available_at='2026-09-16T17:10'),
          dict(base,origin_id='3',timing='prospective',source='live_scan',available_at='2026-09-16T17:40'),
          dict(base,origin_id='1',timing='prospective',source='live_scan',available_at='2026-09-16T16:48'),
          dict(base,origin_id='4',timing='prospective',source='manual_recommendation',available_at='2026-09-16T17:33'),
          dict(base,origin_id='5',timing='prospective',source='live_scan',available_at='2026-09-16T16:48',lane='rotation')]
    kept=sorted(e['origin_id'] for e in collapse(rows))
    assert kept==['1','4','5']


def test_success_and_failure_retained_not_trade_returns():
    o=origins(sample(),'2026-09-10T16:00:00+08:00','published')[0]
    for close in (110,90):
        e=evaluate(o,['2026-09-11'],[dict(trading_date='2026-09-11',close=close,high=110,low=90,open=100)],'2026-09-11')
        assert e['windows']['1']['return_pct']==(10 if close==110 else -10)
        assert e['path_check']=='both_touched_order_unknown'
        assert e['execution_status']=='not_verified'
        assert e['windows']['3']['status']=='not_due'


def test_missing_session_is_not_skipped_and_future_is_ignored():
    o=origins(sample(),'2026-09-10T16:00:00+08:00','published')[0]
    e=evaluate(o,['2026-09-11','2026-09-14'],[dict(trading_date='2026-09-14',close=130)],'2026-09-11')
    assert e['windows']['1']['status']=='missing'
    assert e['latest_close'] is None


def test_hindsight_is_marked_and_does_not_become_live_signal():
    o=origins(sample(),'2026-09-11T16:00:00+08:00','historical_report')[0]
    assert o['timing']=='reconstructed'
    assert not o['trade_authorized']

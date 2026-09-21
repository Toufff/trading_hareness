from app.short_term_lanes.tracking_rules import evaluate


def origin():
    return dict(origin_id='a',symbol='X',name='样本',lane='trend',signal_date='2026-09-10',
                close=100,reference_high=110,reference_low=90,rank=1,timing='prospective',
                confirmation='确认',invalidation='失效')


def test_split_preserves_economic_return_and_original_structure():
    rows=[dict(trading_date='2026-09-10',close=100,adj_factor=1),
          dict(trading_date='2026-09-11',close=50,high=52,low=48,adj_factor=2,pre_close=50)]
    r=evaluate(origin(),['2026-09-11'],rows,'2026-09-11')
    assert r['windows']['1']['return_pct']==0
    assert r['path_check']=='no_reference_touch'
    assert r['latest_close']==50
    assert r['series'][0]['close']==50
    assert r['series'][0]['adjusted_close_on_origin_basis']==100


def test_missing_factor_is_unknown_not_negative_return():
    rows=[dict(trading_date='2026-09-10',close=100,adj_factor=1),
          dict(trading_date='2026-09-11',close=50,high=52,low=48,pre_close=50)]
    r=evaluate(origin(),['2026-09-11'],rows,'2026-09-11')
    assert r['windows']['1']['return_pct'] is None
    assert r['adjustment_status']=='incomplete'
    assert r['path_check']=='adjustment_unverified'


def test_price_works_without_moneyflow():
    rows=[dict(trading_date='2026-09-10',close=100,adj_factor=1),
          dict(trading_date='2026-09-11',close=105,high=106,low=100,adj_factor=1)]
    r=evaluate(origin(),['2026-09-11'],rows,'2026-09-11')
    assert r['windows']['1']['return_pct']==5
    assert r['series'][0]['main_net'] is None
    assert not r['missing_dates']

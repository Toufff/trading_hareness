from app.short_term_lanes.virtual_entry import freeze, evaluate


def case():
    origin = dict(signal_date='2026-09-01', close=10, reference_high=11, reference_low=9)
    origin['virtual_entry_contract'] = freeze(origin, {'amount':100, 'amount_multiple':1}, '2026-09-01T16:00:00+08:00')
    days = ['2026-09-'+d for d in ('02','03','04','07','08','09')]
    bars = [dict(trading_date=d, open=11.5, high=12, low=11.1, close=11.7, amount=120,
                 limit_up=13, limit_down=8, is_suspended=False, adj_factor=1) for d in days]
    return origin, days, bars


def test_b_point_is_not_discovery_close_and_costs_are_charged():
    origin, days, bars = case()
    r = evaluate(origin, days, bars, bars, days[-1])
    assert r['state'] == 'simulated'
    assert r['signal_date'] == days[0]
    assert r['execution']['entry_date'] == days[1]
    assert r['execution']['costs']['slippage_bps'] > 0
    assert not r['actual_trade']


def test_limit_locked_tianrongxin_style_does_not_invent_fill():
    o, ds, bs = case(); bs[1]['open'] = bs[1]['high'] = bs[1]['low'] = bs[1]['close'] = 13
    r = evaluate(o, ds, bs, bs, ds[-1])
    assert r['state'] == 'execution_blocked'
    assert r['execution']['status'] == 'entry_limit_blocked'


def test_invalidated_failure_case_and_missing_intermediate_bar():
    o, ds, bs = case(); bs[0]['close'] = 8.5
    assert evaluate(o, ds, bs, bs, ds[-1])['state'] == 'invalidated'
    assert evaluate(o, ds, bs[1:], bs[1:], ds[-1])['state'] == 'data_gap'


def test_legacy_and_no_same_day_fill():
    o, ds, bs = case()
    assert evaluate(o, ds[:1], bs[:1], bs[:1], ds[0])['state'] == 'awaiting_next_session'
    o.pop('virtual_entry_contract')
    assert evaluate(o, ds, bs, bs, ds[-1])['state'] == 'unregistered'

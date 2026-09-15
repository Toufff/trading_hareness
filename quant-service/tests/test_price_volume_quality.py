"""Adversarial price-volume examples; no assertion of trading profitability."""
from dataclasses import FrozenInstanceError
import pytest
from app.short_term_lanes.price_volume import PriceVolumeSettings, assess, segment_evidence
from app.short_term_lanes.advanced_strategies import evaluate, ohlc_features
from app.short_term_lanes.conditions import watch_conditions


def base(prices=None, amounts=None):
    prices = prices or [10, 10.2, 10.4, 10.6, 10.8, 11, 11.2, 11.8, 12, 12.1, 11.9]
    amounts = amounts or [5e8] * 10 + [5.2e8]
    return dict(close=prices[-1], change_pct=(prices[-1]/prices[-2]-1)*100,
                amount=amounts[-1], amount_multiple=1.04, return_10d=19,
                prior_high=max(prices[-6:-1]), recent_low=min(prices[-5:]), ma5=11.96, ma10=11.5,
                flow_series=[dict(date=f'2026-09-{i+1:02}', close=p, amount=a) for i,(p,a) in enumerate(zip(prices,amounts))])


def latest_bar(high=12.0, close=11.9):
    return [dict(date='2026-09-11', open=11.7, high=high, low=11.6, close=close, amount=5.2e8)]


def test_equal_or_higher_amount_cannot_be_called_shrinking_pullback():
    evidence = assess('pullback', base(), latest_bar(), '2026-09-11')
    assert evidence['metrics']['pullback_amount_ratio'] == pytest.approx(1.04)
    assert evidence['metrics']['pullback_amount_contracted'] is False
    assert evidence['status'] == 'quality_warning'


def test_equal_duration_segment_means_not_different_length_totals():
    result = segment_evidence(base(amounts=[5e8]*10+[3.5e8])['flow_series'])
    assert result['pullback_amount_ratio'] == pytest.approx(.7)
    assert result['pullback_sessions'] == 1
    assert result['advance_sessions'] > 1


def test_long_upper_wick_changes_quality_without_fabricating_intraday():
    good = assess('expansion', base(), latest_bar(), '2026-09-11')
    bad = assess('expansion', base(), latest_bar(high=14), '2026-09-11')
    assert good['metrics']['close_location'] > bad['metrics']['close_location']
    assert bad['status'] == 'quality_warning'
    assert 'intraday_order' in bad['unverified']


def test_stale_or_inconsistent_ohlc_is_unknown_not_passed():
    for bars in ([], [{**latest_bar()[0], 'date':'2026-09-10'}], latest_bar(close=12.5)):
        value = assess('expansion', base(), bars, '2026-09-11')
        assert value['status'] == 'quality_unverified'
        assert value['quality_confirmed'] is False


def test_money_not_mislabelled_as_share_volume_and_relay_unverified():
    value = assess('relay', base(), latest_bar(), '2026-09-11')
    assert value['metrics']['volume_multiple'] is None
    assert 'limit_board_process' in value['unverified']
    assert value['quality_confirmed'] is False


def test_contraction_conditions_use_same_ohlc_reference_not_close_platform():
    values = {**base(), 'advanced': {'prior10_high':12.3, 'prior10_low':11.2, 'first_close_breakout':False}}
    confirm, invalid = watch_conditions('contraction', values)
    assert '12.30' in confirm and '12.30' in invalid
    assert '11.20' in invalid
    assert '已首次' not in confirm


def test_config_is_frozen():
    with pytest.raises(FrozenInstanceError):
        PriceVolumeSettings().pullback_contraction_ratio = 2


def test_tiny_bounce_is_retained_as_raw_evidence_not_strong_reclaim():
    from test_short_term_advanced_strategies import bars
    history = bars(reclaim=True)
    # Old historical low below the whole sell-off made the old predicate pass.
    history[-8].update(low=8.9)
    history[-1].update(open=9.5, high=9.6, low=9.45, close=9.51, amount=3.6e8)
    f = ohlc_features(history, history[-1]['date'])
    matched, _, reason, details = evaluate('reclaim', {}, ohlc=f,
        sector_rotation={'latest_breadth':.6}, events=[], verified_negative=False)
    assert not matched
    assert details['raw_match'] is True
    assert details['recovery_fraction'] < .03
    assert '仅弱反弹' in reason


def test_pressure_approach_is_not_first_breakout_and_line_is_identical():
    from test_short_term_advanced_strategies import bars
    history = bars()
    line = max(r['high'] for r in history[-11:-1])
    history[-1].update(close=line*.999, high=line*1.01)
    f = ohlc_features(history, history[-1]['date'])
    assert f['first_close_breakout'] is False
    _matched, _, _reason, details = evaluate('contraction', {}, ohlc=f,
        sector_rotation={'latest_breadth':.6}, events=[], verified_negative=False)
    confirm, invalid = watch_conditions('contraction', {**base(),'advanced':details})
    assert f'{line:.2f}' in confirm and f'{line:.2f}' in invalid


def test_each_lane_receives_daily_quality_without_granting_execution():
    for lane in ('accumulation','expansion','pullback','trend','event','relay','contraction','rotation','reclaim'):
        value = assess(lane, base(), latest_bar(), '2026-09-11')
        assert value['version'] and value['references']
        assert 'execution' in value['unverified']
        assert value['metrics']['close_location'] is not None


def test_long_wick_downgrades_screen_not_raw_candidate_or_tracking():
    from test_short_term_lanes import ShortTermLaneTests
    from app.short_term_lanes.rules import Settings, screen
    rows, sessions = ShortTermLaneTests().peer_market(
        [10,10.1,10.4,10.6,10.8,11,11.1,11.3,11.4,11.5,12], [5e8]*10+[8e8])
    raw = screen(rows, sessions, sessions[-1], settings=Settings(minimum_universe=1))
    histories = {'002000.SZ':[dict(date=sessions[-1],open=11.8,high=14,low=11,close=12,amount=8e8)]}
    checked = screen(rows, sessions, sessions[-1], price_histories=histories,
                     settings=Settings(minimum_universe=1))
    a, b = raw['lanes'][1], checked['lanes'][1]
    assert a['total_matches'] == b['total_matches']
    candidate = next(x for x in b['tracking_candidates'] if x['symbol']=='002000.SZ')
    assert candidate['state']=='quality_warning'
    assert candidate['price_volume']['quality_confirmed'] is False
    assert all(x['symbol']!='002000.SZ' for x in b['selected'])


def test_missing_ohlc_remains_observation_not_false_quality_pass():
    from test_short_term_lanes import fixture
    from app.short_term_lanes.rules import Settings, screen
    rows, sessions = fixture([10]*11)
    result = screen(rows, sessions, sessions[-1], settings=Settings(minimum_universe=1))
    candidate = result['lanes'][0]['selected'][0]
    assert candidate['price_volume']['status']=='quality_unverified'
    assert candidate['price_volume']['quality_confirmed'] is False
    assert candidate['buy_authorized'] is False

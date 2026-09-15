from app.short_term_lanes.conditions import watch_conditions


M = dict(close=10.17, prior_high=9.83, recent_low=9.46, ma5=9.7, ma10=9.2)


def test_already_broken_platform_is_not_a_future_breakout():
    confirmation, invalidation = watch_conditions('accumulation', M)
    assert '已突破' in confirmation and '9.83' in confirmation
    assert '9.83' in invalidation and '9.46' not in invalidation


def test_unbroken_platform_keeps_a_future_trigger():
    confirmation, _ = watch_conditions('accumulation', {**M, 'close': 9.7})
    assert confirmation.startswith('有效突破9.83')


def test_relay_failure_is_not_a_distant_historical_low():
    _, invalidation = watch_conditions('relay', M)
    assert '回封失败' in invalidation and '9.46' not in invalidation


def test_trend_pullback_reference_is_below_current_price():
    confirmation, _ = watch_conditions('trend', {**M, 'prior_high': 12})
    assert '9.70' in confirmation and '12.00' not in confirmation

from app.ranking_factors.small_cap import band_score, context


def test_soft_band_not_microcap_race():
    assert band_score(100e8)==band_score(300e8)==band_score(600e8)==100
    assert band_score(98.24e8)>99
    assert band_score(50e8)==band_score(1200e8)==50
    assert band_score(20e8)<band_score(50e8)
    assert band_score(610e8)>99


def test_band_independent_of_peer_liquidity_and_float_ratio():
    u={str(i):dict(trade_date='20260911',total_mv=98.24e8,circ_mv=2e8+i) for i in range(25)}
    a=context(u,'2026-09-11')['evidence']['0']['score']
    u['0']['circ_mv']=98.24e8
    u.update({str(i):dict(trade_date='20260911',total_mv=1e9,circ_mv=1e9) for i in range(25,80)})
    assert context(u,'2026-09-11')['evidence']['0']['score']==a

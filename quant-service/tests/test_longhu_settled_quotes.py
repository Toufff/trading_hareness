from datetime import date
from app.longhu_settled_quotes import normalized_quote

def test_uses_requested_historical_day_not_latest_live_day():
    rows={'x':['20260908','20260909','20260910'], 'y':[[8,7.75,8.08,7.51],[7.5,7.63,8.03,7.46],[8,8,9,7]],'vol':[100,200,300],'bal':[1000,2000,3000]}
    q=normalized_quote('600664.SH',date(2026,9,9),rows)
    assert q['close']==7.63 and q['pre_close']==7.75 and q['amount']==2000
    assert q['trade_date']=='20260909'

def test_missing_day_or_bad_candle_is_not_synthesized():
    assert normalized_quote('600664.SH',date(2026,9,9),{'x':['20260908'],'y':[[8,8,8,8]]}) is None
    assert normalized_quote('600664.SH',date(2026,9,9),{'x':['20260908','20260909'],'y':[[8,8,8,8],[8,9,7,6]]}) is None

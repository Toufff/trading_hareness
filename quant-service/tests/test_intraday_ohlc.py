import pytest
from app.intraday_scan.ohlc import merge

def inputs():
    return ({'600000.SH':[dict(date='2026-09-11',close=10)]},
            [dict(ts_code='600000.SH',trade_date='20260914',trade_time='20260914145100',
                  open=10,high=11,low=9.9,price=10.5,amount=5e8,pre_close=10)],
            '2026-09-14T14:50:00+08:00','2026-09-14T14:52:00+08:00')

def test_real_forming_ohlc_not_minute_inferred():
    h,q,c,t=inputs();bars,health=merge(h,q,c,t)
    assert h['600000.SH'][-1]['date']=='2026-09-11'
    assert bars['600000.SH'][-1]['high']==11 and bars['600000.SH'][-1]['provisional']
    assert health['current_day_ready']==1 and health['ready']==0

@pytest.mark.parametrize('field,value',[('trade_date','20260911'),('high',9),('amount',None),('pre_close',20),('trade_time','20260914140000')])
def test_wrong_or_stale_quote_not_claimed_current(field,value):
    h,q,c,t=inputs();q[0][field]=value;bars,health=merge(h,q,c,t)
    assert health['current_day_ready']==0 and bars==h and health['errors']

def test_afterclose_quote_cannot_repair_preclose_history():
    h,q,c,t=inputs()
    with pytest.raises(ValueError):merge(h,q,c,'2026-09-14T15:30:00+08:00')

def test_lunch_break_capture_can_use_the_1130_market_window():
    h,q,_,_=inputs()
    q[0]['trade_time']='20260914123300'
    bars,health=merge(h,q,'2026-09-14T11:30:00+08:00','2026-09-14T12:33:00+08:00')
    assert bars['600000.SH'][-1]['date']=='2026-09-14'
    assert bars['600000.SH'][-1]['available_at']=='2026-09-14T12:33:00+08:00'
    assert health['current_day_ready']==1

def test_lunch_break_accepts_the_last_exchange_timestamp():
    h,q,_,_=inputs()
    q[0]['trade_time']='20260914113000'
    _,health=merge(h,q,'2026-09-14T11:30:00+08:00','2026-09-14T12:59:00+08:00')
    assert health['current_day_ready']==1

def test_1130_window_cannot_be_created_after_afternoon_trading_resumes():
    h,q,_,_=inputs()
    q[0]['trade_time']='20260914113000'
    with pytest.raises(ValueError):
        merge(h,q,'2026-09-14T11:30:00+08:00','2026-09-14T13:00:00+08:00')

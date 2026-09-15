from unittest.mock import patch
import pytest
from app.intraday_scan.tail import build

def sample():
    return dict(cutoff='2026-09-14T14:45:00+08:00',observed_at='2026-09-14T14:49:00+08:00',
        sessions=['2026-09-11'],history=[dict(trade_date='20260911',symbol='600000.SH')],
        rows=[dict(trade_date='20260914',symbol='600000.SH',main_net=123)],health=dict(plate_coverage=1))

def test_today_included_without_mutating_source():
    import copy
    d=sample();before=copy.deepcopy(d)
    with patch('app.intraday_scan.tail.screen',return_value=dict(status='completed',lanes=[])) as screen:
        r=build(d)
    assert screen.call_args.args[0][-1]['main_net']==123
    assert screen.call_args.args[1][-1]=='2026-09-14'
    assert d==before and r['settled'] is False and r['daily_writes'] is False

@pytest.mark.parametrize('field,value',[('cutoff','2026-09-14T12:00:00+08:00'),('cutoff','2026-09-14T15:00:00+08:00'),('sessions',['2026-09-14'])])
def test_no_midday_or_settled_masquerade(field,value):
    d=sample();d[field]=value
    with pytest.raises(ValueError):build(d)

def test_partial_history_not_success():
    with patch('app.intraday_scan.tail.screen',return_value=dict(status='data_gap')):
        with pytest.raises(ValueError):build(sample())

def test_settings_roundtrip():
    from dataclasses import asdict
    from app.intraday_scan.tail import restore_settings
    from app.short_term_lanes.rules import Settings
    from app.ranking_factors import FactorSpec
    s=Settings(ranking_factors=(FactorSpec('small_cap'),))
    assert restore_settings(asdict(s))==s

def test_wrong_live_date_rejected():
    d=sample();d['rows'][0]['trade_date']='20260911'
    with pytest.raises(ValueError):build(d)

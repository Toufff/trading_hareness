from datetime import datetime
from contextlib import contextmanager
import pytest

from app.event_research.schedule import targets, due_slot, delivery_state, scan_refresh_allowed
from app.event_research import delivery


def dt(value):
    return datetime.fromisoformat(value)


def test_weekday_and_weekend_targets():
    assert [t.hour for t in targets(dt('2026-09-15').date())] == [9, 12, 22]
    weekend = [t for day in range(18, 22) for t in targets(dt(f'2026-09-{day}').date())
               if dt('2026-09-18T15:00+08:00') <= t <= dt('2026-09-21T09:00+08:00')]
    assert [(t.day, t.hour) for t in weekend] == [(18, 22), (20, 22), (21, 9)]


@pytest.mark.parametrize('start,end,expected', [
    ('2026-09-24','2026-09-28', [('2026-09-24',22),('2026-09-27',22),('2026-09-28',9)]),
    ('2026-09-30','2026-10-08', [('2026-09-30',22),('2026-10-07',22),('2026-10-08',9)]),
    ('2026-02-13','2026-02-24', [('2026-02-13',22),('2026-02-23',22),('2026-02-24',9)]),
])
def test_exact_three_closure_endpoints(start,end,expected):
    from datetime import timedelta
    first,last=dt(start).date(),dt(end).date()
    result=[t for offset in range((last-first).days+1)
            for t in targets(first+timedelta(days=offset))
            if dt(start+'T15:00+08:00') <= t <= dt(end+'T09:00+08:00')]
    assert [(t.date().isoformat(),t.hour) for t in result] == expected


def test_makeup_workdays_are_closed_and_holiday_midday_has_no_api_call(tmp_path):
    from app.event_research.trading_calendar import is_open
    for day in ('2026-09-20','2026-10-10','2026-02-14'):
        assert not is_open(dt(day).date())
    for now in ('2026-10-02T11:55+08:00','2026-10-07T11:55+08:00'):
        assert delivery.deliver(None,output_dir=tmp_path,api_base='unused',now=dt(now))['status']=='outside_window'


def test_long_holiday_freshness_and_unknown_calendar():
    from app.event_research.trading_calendar import CalendarUnavailable
    value=dict(status='analyzed',analysis={'status':'completed'},cutoff='2026-09-30T21:56+08:00')
    state=delivery_state(value,dt('2026-10-06T12:00+08:00'))
    assert not state['stale']
    assert state['schedule']['next_expected_at']=='2026-10-07T22:00:00+08:00'
    assert delivery_state(value,dt('2026-10-07T22:00+08:00'))['stale']
    assert delivery_state(value,dt('2027-01-04T12:00+08:00'))['schedule']['state']=='calendar_unavailable'
    with pytest.raises(CalendarUnavailable):due_slot(dt('2027-01-04T08:55+08:00'))
    assert not scan_refresh_allowed(dt('2027-01-04T12:00+08:00'))


def test_no_duplicate_adjacent_day_night():
    assert [t.hour for t in targets(dt('2026-09-15').date())].count(22)==1


def test_timezone_start_retry_and_no_all_day_backfill():
    assert due_slot(dt('2026-09-15T00:55+00:00')).hour == 9
    assert due_slot(dt('2026-09-15T09:35+08:00')).hour == 9
    assert due_slot(dt('2026-09-15T09:36+08:00')) is None
    assert due_slot(dt('2026-09-19T12:00+08:00')) is None


def test_weekend_scan_cannot_create_extra_news_delivery():
    for value in ('2026-09-18T16:40+08:00','2026-09-19T12:00+08:00',
                  '2026-09-20T22:00+08:00','2026-09-21T08:59+08:00',
                  '2026-09-30T16:40+08:00','2026-10-02T12:00+08:00',
                  '2026-10-07T22:00+08:00','2026-10-08T08:50+08:00'):
        assert not scan_refresh_allowed(dt(value))
    assert scan_refresh_allowed(dt('2026-09-18T12:20+08:00'))
    assert scan_refresh_allowed(dt('2026-09-21T09:00+08:00'))


def test_freshness_matches_cadence_not_one_hour():
    value = dict(status='analyzed', analysis={'status':'completed'}, cutoff='2026-09-15T08:56+08:00')
    assert not delivery_state(value, dt('2026-09-15T11:45+08:00'))['stale']
    assert delivery_state(value, dt('2026-09-15T12:00+08:00'))['stale']
    value['status'] = 'failed'
    assert delivery_state(value, dt('2026-09-15T09:01+08:00'))['stale']


class Database:
    def __init__(self, acquired=True): self.acquired=acquired
    @contextmanager
    def transaction(self): yield self
    def execute(self, *args): return self
    def fetchone(self): return {'acquired':self.acquired}


def test_deliver_fresh_source_semantics_export_readback_and_slot_identity(monkeypatch,tmp_path):
    calls=[]
    result=dict(status='analyzed',run_id='event-123',input_hash='hash',cutoff='2026-09-15T08:56+08:00',
                analysis={'status':'completed'},source_status=[{'status':'ok'}],events=[],leads=[])
    def record(db, **kw):
        assert kw['run_key'].endswith('2026-09-15T09:00:00+08:00')
        return kw['operation']()
    monkeypatch.setattr(delivery,'run_recorded',record)
    def runner(db,**kw):calls.append(('fetch',kw));return result
    def verifier(value,base):calls.append(('verify',value['run_id']))
    out=delivery.deliver(Database(),output_dir=tmp_path,api_base='local',now=dt('2026-09-15T08:55+08:00'),runner=runner,verifier=verifier)
    assert out['status']=='completed'
    assert calls==[('fetch',{'refresh':True}),('verify','event-123')]
    assert (tmp_path/'event-123.md').exists()
    result['analysis']['status']='failed'
    with pytest.raises(RuntimeError,match='semantic_analysis_failed'):
        delivery.deliver(Database(),output_dir=tmp_path,api_base='local',now=dt('2026-09-15T08:55+08:00'),runner=runner,verifier=verifier)


def test_manual_does_not_complete_future_slot_and_concurrency_noop(monkeypatch,tmp_path):
    def record(db,**kw):
        assert ':manual:' in kw['run_key']
        assert kw['input_summary']['target_at'] is None
        return {'status':'completed'}
    monkeypatch.setattr(delivery,'run_recorded',record)
    kwargs=dict(output_dir=tmp_path,api_base='local',now=dt('2026-09-15T02:00+08:00'))
    assert delivery.deliver(Database(),**kwargs)['status']=='outside_window'
    assert delivery.deliver(Database(),manual=True,**kwargs)['status']=='completed'
    assert delivery.deliver(Database(False),manual=True,**kwargs)['status']=='skipped_in_flight'


def test_readback_mismatch_fails(monkeypatch):
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args,**kwargs):return self
        def raise_for_status(self):pass
        def json(self):return {'run_id':'old','input_hash':'wrong'}
    monkeypatch.setattr(delivery.requests,'Session',Session)
    with pytest.raises(RuntimeError,match='readback_mismatch'):
        delivery.verify_readback({'run_id':'new','input_hash':'right'},'http://localhost')

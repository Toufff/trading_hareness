from datetime import datetime,timezone
import pytest
from app.short_term_lanes.event_time import resolve_cutoff,event_available,event_unavailable_reason
from app.short_term_lanes.rules import screen,Settings
from tests.test_short_term_lanes import fixture


def test_default_cutoff_is_close_not_wall_clock_and_requires_timezone():
    assert resolve_cutoff('2026-09-11').isoformat()=='2026-09-11T15:00:00+08:00'
    for value in ('2026-09-11T18:00:00','2026-09-12T09:00:00+08:00'):
        with pytest.raises(ValueError):resolve_cutoff('2026-09-11',value)
    assert resolve_cutoff('2026-09-11','2026-09-11T10:00:00Z').hour==18


def test_published_yesterday_but_verified_later_does_not_leak():
    cutoff=resolve_cutoff('2026-09-11')
    event={'published_date':'2026-09-10','available_at':'2026-09-12T08:00:00+08:00'}
    assert not event_available(event,cutoff)
    assert event_unavailable_reason(event,cutoff)=='future_available_at'
    assert not event_available({**event,'available_at':'2026-09-11T10:00:00'},cutoff)


def test_event_after_close_only_enters_explicit_next_session_research_cutoff():
    target,days=fixture([10]*10+[10.3],amounts=[5e8]*10+[6e8])
    rows=list(target)
    for i in range(1,8):
        peer,_=fixture([10]*11,symbol=f'002{i:03}.SZ');rows+=peer
    event={'verified':True,'url':'https://www.cninfo.com.cn/event.pdf','benefit':'新增订单',
           'published_date':days[-1],'available_at':days[-1]+'T16:00:00+08:00','event_type':'material_contract',
           'surprise':'positive','priced_in':False,'impact_direction':'positive'}
    opts={'events':{'002170.SZ':[event]},'settings':Settings(minimum_universe=1)}
    closing=screen(rows,days,days[-1],**opts)
    evening=screen(rows,days,days[-1],information_cutoff=days[-1]+'T18:00:00+08:00',**opts)
    assert closing['lanes'][4]['total_matches']==0
    assert closing['coverage']['excluded']['event_unavailable:future_available_at']==1
    assert evening['lanes'][4]['total_matches']==1
    assert '下一交易日' in evening['event_time_scope']

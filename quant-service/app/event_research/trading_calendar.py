"""Versioned exchange-notice calendar, not an inference from missing stock bars.

Only explicitly reviewed years are supported. Annual notices are configuration
evidence shipped with the immutable release; no network I/O occurs on API GET.
"""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path


class CalendarUnavailable(ValueError):
    pass


@lru_cache(maxsize=8)
def year_notice(year):
    path = Path(__file__).with_name(f'calendar_{year}.json')
    if not path.exists():
        raise CalendarUnavailable(f'exchange_calendar_unavailable:{year}')
    return json.loads(path.read_text(encoding='utf-8'))


def is_open(day):
    notice = year_notice(day.year)  # Even weekends outside coverage are unknown.
    return day.weekday() < 5 and not any(
        date.fromisoformat(start) <= day <= date.fromisoformat(end)
        for start, end in notice['closed_ranges'])


def metadata(day):
    try:
        notice = year_notice(day.year)
    except CalendarUnavailable:
        return dict(status='missing', year=day.year, reason='交易所年度日历尚未核验，不按星期猜测开市')
    remaining = (date(day.year, 12, 31) - day).days
    return dict(status='expiring' if remaining <= 30 else 'verified',
                source=notice['source'], verified_at=notice['verified_at'],
                coverage_start=f'{day.year}-01-01', coverage_end=f'{day.year}-12-31',
                reason='下一年度日历需要核验更新' if remaining <= 30 else '')

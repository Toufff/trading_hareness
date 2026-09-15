"""Point-in-time event availability for next-session daily research.

The default is exchange close, never current wall time. A later same-day cutoff
must be supplied by the data collector and frozen with the experiment input.
"""
from datetime import date, datetime, time, timedelta, timezone

CHINA = timezone(timedelta(hours=8))


def resolve_cutoff(as_of_date: str, information_cutoff=None) -> datetime:
    day=date.fromisoformat(as_of_date)
    if str(day)!=as_of_date:
        raise ValueError('as_of_date must be strict ISO date')
    if information_cutoff is None:
        return datetime.combine(day,time(15),CHINA)
    value=(datetime.fromisoformat(information_cutoff) if isinstance(information_cutoff,str)
           else information_cutoff)
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('information_cutoff must be a timezone-aware datetime')
    local=value.astimezone(CHINA)
    if local.date()!=day:
        raise ValueError('information_cutoff must be within the as-of exchange date')
    return local


def event_unavailable_reason(event: dict, cutoff: datetime) -> str | None:
    available=event.get('available_at')
    if not isinstance(available,(str,datetime)):
        return 'missing_available_at'
    try:
        instant=datetime.fromisoformat(available) if isinstance(available,str) else available
    except ValueError:
        return 'invalid_available_at'
    if instant.tzinfo is None or instant.utcoffset() is None:
        return 'naive_available_at'
    if instant>cutoff:
        return 'future_available_at'
    try:
        published=date.fromisoformat(str(event.get('published_date','')))
    except ValueError:
        return 'invalid_published_date'
    if published>cutoff.astimezone(CHINA).date():
        return 'future_publication'
    if instant.astimezone(CHINA).date()<published:
        return 'availability_precedes_publication_date'
    return None


def event_available(event: dict, cutoff: datetime) -> bool:
    return event_unavailable_reason(event,cutoff) is None

"""Public news-delivery cadence, in Shanghai time; independent of scan cadence."""
from datetime import datetime, timedelta, timezone
from .trading_calendar import is_open, metadata, CalendarUnavailable

SHANGHAI = timezone(timedelta(hours=8))
LEAD = timedelta(minutes=5)
RETRY_WINDOW = timedelta(minutes=35)


def targets(day):
    # Trading-day night is also the last session's holiday endpoint, once only.
    hours = (9, 12, 22) if is_open(day) else (22,) if is_open(day + timedelta(days=1)) else ()
    return [datetime(day.year, day.month, day.day, hour, tzinfo=SHANGHAI) for hour in hours]


def surrounding(now):
    now = now.astimezone(SHANGHAI)
    slots = []
    for offset in range(-32, 33):
        try:
            slots.extend(targets(now.date() + timedelta(days=offset)))
        except CalendarUnavailable:
            continue  # Bounds remain explicit; never manufacture future slots.
    return sorted(slots)


def due_slot(now):
    local = now.astimezone(SHANGHAI)
    return next((t for t in targets(local.date()) if t - LEAD <= now <= t + RETRY_WINDOW), None)


def scan_refresh_allowed(now):
    """Weekend scans consume dedicated deliveries, not an extra news run.

    Explicit operator `run`/`--manual` remains available. Only opportunistic
    news refresh is suppressed, never the market scan itself.
    """
    local = now.astimezone(SHANGHAI)
    try:
        if not is_open(local.date()):
            return False
        if local.hour >= 15 and not is_open(local.date() + timedelta(days=1)):
            return False
        if local.hour < 9 and not is_open(local.date() - timedelta(days=1)):
            return False
        return True
    except CalendarUnavailable:
        return False


def delivery_state(value, now):
    """A successful collection before the next target is not stale after one hour.

    An ad-hoc failed refresh remains a visible failure even if the previous
    slot succeeded. Never relabel the retained historical events as new.
    """
    slots = surrounding(now)
    previous = max((t for t in slots if t <= now), default=None)
    upcoming = min((t for t in slots if t > now), default=None)
    calendar = metadata(now.astimezone(SHANGHAI).date())
    unavailable = calendar['status'] == 'missing' or previous is None or upcoming is None
    cutoff = datetime.fromisoformat(value['cutoff']) if value.get('cutoff') else None
    valid = (value.get('status') in ('analyzed', 'no_news') and
             value.get('analysis', {}).get('status') != 'failed' and
             all(s.get('status') == 'ok' for s in value.get('source_status', [])))
    stale = unavailable or not valid or cutoff is None or cutoff < previous - LEAD
    return dict(stale=stale, schedule=dict(
        timezone='Asia/Shanghai', trading_day_targets=['09:00', '12:00', '22:00'],
        closure_targets=['最后交易日22:00', '下个交易日前一日22:00', '下个交易日09:00'],
        calendar=calendar,
        last_expected_at=previous.isoformat() if previous else None,
        next_expected_at=upcoming.isoformat() if upcoming else None,
        state='calendar_unavailable' if unavailable else 'overdue_or_failed' if stale else 'current',
        starts_minutes_before=5))

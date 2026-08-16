"""Deterministic China-session clock helpers for live and replay features."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


CN_TZ = ZoneInfo("Asia/Shanghai")
CONTINUOUS_AUCTION_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)


def _as_aware_utc(value: datetime) -> datetime:
    """Make persisted/replay timestamps comparable without using host local time."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def continuous_auction_bounds(value: datetime) -> tuple[datetime, datetime] | None:
    """Return the containing China continuous-auction segment in UTC.

    The return value is deliberately a *single* segment.  A research outcome
    may not quietly bridge the 11:30--13:00 lunch break or the overnight gap:
    that would turn the first later quote into a fake five/15/30-minute exit.
    """
    local = _as_aware_utc(value).astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    for start, end in CONTINUOUS_AUCTION_SESSIONS:
        if start <= clock <= end:
            session_start = datetime.combine(local.date(), start, tzinfo=CN_TZ).astimezone(timezone.utc)
            session_end = datetime.combine(local.date(), end, tzinfo=CN_TZ).astimezone(timezone.utc)
            return session_start, session_end
    return None


def intraday_outcome_window(entry_at: datetime, *, horizon_minutes: int, cutoff: datetime,
                            tolerance_seconds: int = 90) -> dict[str, Any]:
    """Build a bounded quote window for an intraday outcome.

    ``pending`` is reserved for a still-observable target.  Once its segment
    has ended, or a bounded quote-delay tolerance expired, the result becomes
    ``unavailable`` rather than borrowing lunch/overnight prices.  The helper
    is pure so live settlement and deterministic replay share the same clock.
    """
    entry = _as_aware_utc(entry_at)
    as_of = _as_aware_utc(cutoff)
    bounds = continuous_auction_bounds(entry)
    base: dict[str, Any] = {
        "entry_at": entry,
        "cutoff": as_of,
        "horizon_minutes": max(0, int(horizon_minutes)),
        "tolerance_seconds": max(0, int(tolerance_seconds)),
    }
    if bounds is None:
        return {**base, "status": "unavailable", "reason": "entry_outside_continuous_auction"}
    session_start, session_end = bounds
    target_at = entry + timedelta(minutes=base["horizon_minutes"])
    base.update({"session_start": session_start, "session_end": session_end, "target_at": target_at})
    # A target exactly at the close is allowed, but no later quote is allowed.
    if target_at > session_end:
        return {**base, "status": "unavailable", "reason": "target_crosses_continuous_session_boundary"}
    tolerance_end = min(session_end, target_at + timedelta(seconds=base["tolerance_seconds"]))
    if as_of < target_at:
        return {**base, "status": "pending", "reason": "target_not_yet_observable",
                "query_start": target_at, "query_end": as_of}
    if as_of > tolerance_end:
        status, reason = "unavailable", "exit_quote_missing_within_tolerance"
    else:
        status, reason = "pending", "awaiting_exit_quote_within_tolerance"
    return {**base, "status": status, "reason": reason,
            "query_start": target_at, "query_end": min(as_of, tolerance_end),
            "tolerance_end": tolerance_end}


def feature_clock(value: Any) -> time | None:
    """Return a China-session clock from ``HH:MM`` or provider timestamps."""
    text = str(value or "").strip()
    matched = re.findall(r"(?:T|\s)(\d{2}):?(\d{2})(?::\d{2})?(?:\D|$)", text)
    if matched:
        hour, minute = matched[-1]
    else:
        compact = re.fullmatch(r"(\d{2}):?(\d{2})(?::\d{2})?", text)
        if not compact:
            return None
        hour, minute = compact.groups()[:2]
    if not matched:
        matched = [(hour, minute)]
    try:
        return time(int(hour), int(minute))
    except ValueError:
        return None


def eac_window(value: Any) -> str:
    """Classify a minute without treating a late-session spike as fresh momentum."""
    clock = feature_clock(value)
    if clock is None:
        return "unknown"
    if time(9, 40) <= clock <= time(10, 45):
        return "morning"
    if time(13, 0) <= clock <= time(14, 20):
        return "afternoon"
    return "late_or_opening"


def minute_bucket(value: Any) -> str | None:
    clock = feature_clock(value)
    return clock.strftime("%H:%M") if clock is not None else None


__all__ = [
    "CONTINUOUS_AUCTION_SESSIONS", "continuous_auction_bounds", "eac_window",
    "feature_clock", "intraday_outcome_window", "minute_bucket",
]

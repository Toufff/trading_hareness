"""Small, shared A-share market-rule helpers.

These rules are intentionally independent of database access so research,
replay, and execution simulation use the same fallback semantics.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def cn_today(now: datetime | None = None) -> date:
    """Return the exchange calendar date, independent of the container TZ."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).date()


def a_share_limit_ratio(symbol: str, is_st: bool | None = False) -> float:
    """Fallback daily price-band ratio when the exact stk_limit row is absent."""
    code = str(symbol).split(".", 1)[0]
    if is_st:
        return 0.05
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


def is_st_security_name(name: object) -> bool:
    """Identify the exchange's ST prefix without matching incidental letters."""
    value = str(name or "").strip().upper()
    return value.startswith("ST") or value.startswith("*ST")


def china_equity_session(now: datetime | None = None) -> tuple[bool, str]:
    """Return whether a timestamp is within an SSE continuous-auction window."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai"))
    current_time = local.time()
    if local.weekday() >= 5:
        return False, "SSE is closed on weekends"
    if time(9, 30) <= current_time <= time(11, 30) or time(13, 0) <= current_time <= time(15, 0):
        return True, "within SSE continuous auction session"
    return False, "outside SSE continuous auction sessions (09:30-11:30, 13:00-15:00 Asia/Shanghai)"


def china_futures_session(now: datetime | None = None) -> tuple[bool, str]:
    """Conservative daytime guard for the configured CFFEX quote probe."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai"))
    current_time = local.time()
    if local.weekday() >= 5:
        return False, "CFFEX is closed on weekends"
    if time(9, 0) <= current_time <= time(11, 30) or time(13, 30) <= current_time <= time(15, 0):
        return True, "within CFFEX day session"
    return False, "outside CFFEX day sessions (09:00-11:30, 13:30-15:00 Asia/Shanghai)"

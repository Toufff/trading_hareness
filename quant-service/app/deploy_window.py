"""When a release may interrupt the owner surfaces, and when it may not.

A deploy costs the external consumer about fifteen seconds (migration
20260920_0107 has the measurements).  Fifteen seconds is nothing at 22:00 and
a lost collection cycle at 14:59, so the policy is about *when*, not *how
long*.  Two windows are closed:

* **The session**, 09:15-15:10 Asia/Shanghai on a trading day.  It starts
  before the open because the call auction is already running at 09:15, and
  ends after the close because the post-close capture is still draining.
* **The peer batch window**, 06:30-08:00 on weekdays, which is the slot the
  consumer's own daily backfill was moved into.

Both are advisory in the sense that an operator can override them -- a fix
that must ship during the session is a real thing -- but the override has to be
deliberate, and the refusal names which window it is so the operator knows what
they are stepping on.

The exchange calendar refuses to guess for a year nobody has verified.  Here
that would block every deploy until someone files a notice, so an unverified
year falls back to "weekdays are trading days" and says so: the guard errs
toward closing the window, never toward opening it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .event_research.trading_calendar import CalendarUnavailable, is_open

SHANGHAI = ZoneInfo("Asia/Shanghai")

SESSION_START = time(9, 15)
SESSION_END = time(15, 10)

PEER_BATCH_START = time(6, 30)
PEER_BATCH_END = time(8, 0)


@dataclass(frozen=True)
class WindowDecision:
    allowed: bool
    code: str
    reason: str
    local_time: str
    calendar_verified: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "local_time": self.local_time,
            "calendar_verified": self.calendar_verified,
        }


def _trading_day(moment: datetime) -> tuple[bool, bool]:
    """``(is a trading day, the calendar knows the year)``."""
    try:
        return is_open(moment.date()), True
    except CalendarUnavailable:
        return moment.weekday() < 5, False


def evaluate(moment: datetime | None = None) -> WindowDecision:
    """Decide whether a release may run now."""
    now = (moment or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    clock = now.time()
    local = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    trading, verified = _trading_day(now)
    suffix = "" if verified else "（该年度交易所日历未核验，按工作日保守处理）"

    if trading and SESSION_START <= clock < SESSION_END:
        return WindowDecision(
            False, "trading_session",
            f"交易时段 {SESSION_START:%H:%M}-{SESSION_END:%H:%M} 内不发布：一次发布会中断 "
            f"HTTP 面约 10 秒、隧道约 7 秒，落在盘中就是一次采集周期的丢失{suffix}",
            local, verified)

    if now.weekday() < 5 and PEER_BATCH_START <= clock < PEER_BATCH_END:
        return WindowDecision(
            False, "peer_batch_window",
            f"协作方批量窗口 {PEER_BATCH_START:%H:%M}-{PEER_BATCH_END:%H:%M} 内不发布："
            "他的每日回补正在这个槽位运行",
            local, verified)

    return WindowDecision(
        True, "open",
        "非交易时段且不在协作方批量窗口内" if trading else "非交易日",
        local, verified)


__all__ = [
    "PEER_BATCH_END",
    "PEER_BATCH_START",
    "SESSION_END",
    "SESSION_START",
    "SHANGHAI",
    "WindowDecision",
    "evaluate",
]

"""Small, shared A-share market-rule helpers.

These rules are intentionally independent of database access so research,
replay, and execution simulation use the same fallback semantics.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

#: Absolute yuan tolerance a price may sit below/above a limit and still count
#: as sealed.  Kept absolute (not a relative ``*0.999``/``*1.001`` factor) so a
#: 100-yuan name is not given ten ticks of slack.
LIMIT_TOLERANCE = 0.005

#: The price tick every A-share board quotes to.
PRICE_TICK = Decimal("0.01")

_BARE_CODE_RE = re.compile(r"(\d{6})")


def cn_today(now: datetime | None = None) -> date:
    """Return the exchange calendar date, independent of the container TZ."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).date()


def _bare_code(symbol: str) -> str:
    """Extract the 6-digit exchange code regardless of prefix/suffix dressing."""
    match = _BARE_CODE_RE.search(str(symbol or ""))
    return match.group(1) if match else str(symbol or "")


def a_share_limit_ratio(symbol: str, is_st: bool | None = False) -> float:
    """The single source of truth for an A-share name's daily price-band ratio.

    Board rule takes precedence over the ST discount: Beijing Stock Exchange
    names always trade a 30% band, and the ChiNext/STAR (300/301/688/689)
    registration boards always trade a 20% band -- including their own ST
    names, which do NOT get the mainboard's narrower 5% band.  Only a
    mainboard (60/00 prefix) ST name is discounted to 5%.
    """
    code = _bare_code(symbol)
    if code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if is_st:
        return 0.05
    return 0.10


def a_share_limit_prices(
    symbol: str, pre_close: Decimal, *, is_st: bool | None = False,
) -> tuple[Decimal, Decimal]:
    """``(limit_up, limit_down)`` for one session, with each board's own rounding.

    Shanghai and Shenzhen round the band half-up to the tick.  Beijing rounds
    **inward** -- the up limit floored, the down limit ceiled -- which is one
    full tick narrower whenever the two rules disagree.

    That is measured, not assumed.  Across every Beijing session since
    2026-01-01 where the two rules give different answers *and* the day's
    extreme actually reached a limit, the traded extreme matched the inward
    value 70 times out of 70 on the up side and 8 out of 8 on the down side,
    and the half-up value not once.  The same rule reproduces tushare's own
    ``stk_limit`` Beijing rows exactly.

    One tick is not cosmetic here.  The limit price decides whether a bar is
    classified as sealed, and that decides fillability -- so a band one tick
    too wide silently reports a limit-up name as tradable.
    """
    ratio = Decimal(str(a_share_limit_ratio(symbol, is_st=is_st)))
    up = pre_close * (Decimal("1") + ratio)
    down = pre_close * (Decimal("1") - ratio)
    if _bare_code(symbol).startswith(("4", "8", "92")):
        return (up.quantize(PRICE_TICK, rounding=ROUND_FLOOR),
                down.quantize(PRICE_TICK, rounding=ROUND_CEILING))
    return (up.quantize(PRICE_TICK, rounding=ROUND_HALF_UP),
            down.quantize(PRICE_TICK, rounding=ROUND_HALF_UP))


def is_at_limit(price: float | None, limit_price: float | None, tolerance: float = LIMIT_TOLERANCE) -> bool:
    """Whether ``price`` has reached ``limit_price`` within an absolute tolerance.

    Uses an absolute yuan tolerance rather than a relative factor (``*0.999``)
    so the check does not grant a high-priced name many extra ticks of slack.
    """
    if price is None or limit_price is None:
        return False
    return float(price) >= float(limit_price) - tolerance


def is_trading_day(value: date) -> bool:
    """Best-effort trading-day check.

    This is weekday-only and does not consult a CN exchange holiday
    calendar, matching the same simplification ``china_equity_session``
    already makes.  It exists so callers stop writing a non-trading day's
    (weekend's) stale "live" fetch under the calling date's own timestamp.
    """
    return value.weekday() < 5


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

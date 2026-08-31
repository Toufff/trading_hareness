"""Pure China-market intraday cadence and freshness rules."""

from __future__ import annotations

import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


CN_TZ = ZoneInfo("Asia/Shanghai")
INTRADAY_WATCHLIST_MAX_SYMBOLS = 40


def intraday_scan_interval_seconds() -> int:
    try:
        return max(0, min(300, int(os.getenv("INTRADAY_SCAN_INTERVAL_SECONDS", "0"))))
    except ValueError:
        return 0


def intraday_high_frequency_window(now: datetime | None = None) -> bool:
    """Return whether the user-selected high-value continuous-auction window is active."""
    local = (now or datetime.now(timezone.utc)).astimezone(CN_TZ)
    windows = ((time(9, 30), time(10, 0)), (time(11, 10), time(11, 30)),
               (time(13, 0), time(13, 30)), (time(14, 30), time(15, 0)))
    return any(start <= local.time() < end for start, end in windows)


def intraday_effective_scan_interval_seconds(normal_interval_seconds: int, now: datetime | None = None) -> int:
    if normal_interval_seconds <= 0:
        return 0
    return 10 if intraday_high_frequency_window(now) else normal_interval_seconds


def intraday_watchlist_capacity(symbol_count: int, *, max_symbols: int = INTRADAY_WATCHLIST_MAX_SYMBOLS) -> dict[str, int | bool | str]:
    """Fail closed rather than silently scanning only a prefix of a watchlist."""
    requested = max(0, int(symbol_count))
    capacity = max(1, int(max_symbols))
    blocked = requested > capacity
    return {
        "requested_symbols": requested,
        "max_symbols": capacity,
        "blocked": blocked,
        "reason": (
            f"enabled watchlist exceeds the audited {capacity}-symbol realtime coverage cap"
            if blocked else "within audited realtime coverage cap"
        ),
    }


def intraday_next_realtime_validation_offset(current_offset: int, step: int, slots: int = 20) -> int:
    # The scanner itself has a 40-symbol explicit-watch bound.  Do not silently
    # fold a larger enabled basket back to the first 20 symbols: callers pass
    # their audited coverage cap and expose any remaining truncation.
    bounded_slots = max(1, min(100, slots))
    return (max(0, current_offset) + max(0, step)) % bounded_slots


def intraday_realtime_validation_slice(symbols: list[str], current_offset: int,
                                       limit: int) -> tuple[list[str], int]:
    """Return one fair, bounded Super GET minute-validation slice.

    The rotation has to use the *actual* explicit watchlist size.  Advancing
    against a fixed capacity (for example 40 slots for a 36-symbol basket)
    skips its tail forever.  A zero limit intentionally preserves the cursor
    during the special windows, where the dedicated ``rt_k`` loop owns the
    Super GET budget.
    """
    if not symbols:
        return [], 0
    size = len(symbols)
    start = max(0, current_offset) % size
    bounded_limit = max(0, min(int(limit), size))
    if bounded_limit == 0:
        return [], start
    rotated = symbols[start:] + symbols[:start]
    return rotated[:bounded_limit], (start + bounded_limit) % size


def intraday_super_get_fast_interval_seconds() -> float:
    try:
        return max(1.0, min(10.0, float(os.getenv("INTRADAY_SUPER_GET_FAST_INTERVAL_SECONDS", "1"))))
    except ValueError:
        return 1.0


def intraday_super_get_fast_max_in_flight() -> int:
    try:
        return max(1, min(20, int(os.getenv("INTRADAY_SUPER_GET_FAST_MAX_IN_FLIGHT", "20"))))
    except ValueError:
        return 20


def intraday_super_get_fast_max_symbols() -> int:
    """Cap only the explicitly configured fast-validation basket.

    A full watch scan remains capped elsewhere.  Keeping this configurable
    lets service health report when a user intentionally chose lower coverage.
    """
    try:
        return max(1, min(40, int(os.getenv("INTRADAY_SUPER_GET_FAST_MAX_SYMBOLS", "40"))))
    except ValueError:
        return 40


def intraday_fast_quote_retention_days() -> int:
    try:
        return max(1, min(400, int(os.getenv("INTRADAY_FAST_QUOTE_RETENTION_DAYS", "7"))))
    except ValueError:
        return 7


# Retention upper bounds are wide because the statistical gates, not disk, decide
# how much evidence is worth keeping: formal validation needs 60 trading days and
# 200 matured signals, so a 120-day ceiling left no headroom to accumulate a
# sample. History that exceeds the hot window is archived to cold storage rather
# than discarded.
def intraday_rule_input_retention_days() -> int:
    """Keep enough frozen live inputs for the 60-day replay gate, bounded by SSD policy."""
    try:
        value = int(os.getenv("INTRADAY_RULE_INPUT_RETENTION_DAYS", "90"))
    except ValueError:
        value = 90
    return min(400, max(60, value))


def intraday_session_elapsed_seconds(now: datetime) -> float | None:
    local = now.astimezone(CN_TZ)
    clock = local.time()
    segment_start = time(9, 30) if time(9, 30) <= clock < time(11, 30) else time(13, 0) if time(13, 0) <= clock < time(15, 0) else None
    if segment_start is None:
        return None
    return max(0.0, (local - datetime.combine(local.date(), segment_start, tzinfo=local.tzinfo)).total_seconds())


def intraday_runtime_service_state(*, configured: bool, expected_active: bool,
                                   last_observed_at: datetime | None, observed_at: datetime,
                                   max_age_seconds: float, startup_grace_seconds: float = 60.0) -> tuple[str, float | None]:
    """Classify runtime freshness without treating configuration as health."""
    if not configured:
        return "disabled", None
    if not expected_active:
        age = max(0.0, (observed_at - last_observed_at).total_seconds()) if last_observed_at else None
        return "standby", age
    if last_observed_at is None:
        elapsed = intraday_session_elapsed_seconds(observed_at)
        return ("starting" if elapsed is not None and elapsed <= startup_grace_seconds else "degraded"), None
    age = max(0.0, (observed_at - last_observed_at).total_seconds())
    if age <= max_age_seconds:
        return "healthy", age
    elapsed = intraday_session_elapsed_seconds(observed_at)
    return ("starting", age) if elapsed is not None and elapsed <= startup_grace_seconds else ("degraded", age)


def intraday_next_monitor_delay_seconds(normal_interval_seconds: int, now: datetime | None = None) -> float:
    if normal_interval_seconds <= 0:
        return 0
    local = (now or datetime.now(timezone.utc)).astimezone(CN_TZ)
    normal_delay = float(intraday_effective_scan_interval_seconds(normal_interval_seconds, local))
    if intraday_high_frequency_window(local):
        return normal_delay
    for start in (time(9, 30), time(11, 10), time(13, 0), time(14, 30)):
        seconds = (datetime.combine(local.date(), start, tzinfo=local.tzinfo) - local).total_seconds()
        if 0 < seconds < normal_delay:
            return seconds
    return normal_delay


def intraday_board_refresh_interval_seconds(now: datetime | None = None) -> int:
    """Refresh slow board funds at most once per minute in key windows."""
    return 60 if intraday_high_frequency_window(now) else 300


def intraday_board_curve_enabled() -> bool:
    return os.getenv("INTRADAY_BOARD_CURVE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def intraday_board_curve_retention_days() -> int:
    try:
        value = int(os.getenv("INTRADAY_BOARD_CURVE_RETENTION_DAYS", "60"))
    except ValueError:
        value = 60
    return min(366, max(7, value))


def intraday_board_rotation_retention_days() -> int:
    """Retain reconstructible board-rotation events for a bounded review window."""
    try:
        value = int(os.getenv("INTRADAY_BOARD_ROTATION_RETENTION_DAYS", "60"))
    except ValueError:
        value = 60
    return min(366, max(7, value))


def intraday_board_curve_clock_session(now: datetime | None = None) -> tuple[bool, str]:
    """Use the SSE observation clock (09:20-11:30, 13:00-15:00)."""
    local = (now or datetime.now(timezone.utc)).astimezone(CN_TZ)
    if local.weekday() >= 5:
        return False, "SSE is closed on weekends"
    current = local.time()
    if time(9, 20) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0):
        return True, "within SSE board observation sessions (09:20-11:30, 13:00-15:00 Asia/Shanghai)"
    return False, "outside SSE board observation sessions (09:20-11:30, 13:00-15:00 Asia/Shanghai)"

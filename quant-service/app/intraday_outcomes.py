"""Pure settlement primitives shared by intraday replay and validation."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo


INTRADAY_OUTCOME_HORIZONS: tuple[tuple[str, int], ...] = (("5m", 5), ("15m", 15), ("30m", 30))


def intraday_signal_direction(signal_type: str) -> int | None:
    """Express an alert research hypothesis as a signed return direction."""
    if signal_type in {"entry", "watch"}:
        return 1
    if signal_type in {"reduce", "exit"}:
        return -1
    return None


def intraday_signal_outcome_metrics(entry_price: Decimal, direction: int,
                                    prices: list[Decimal]) -> dict[str, Decimal] | None:
    """Measure an observed path without implying a tradable fill."""
    if entry_price <= 0 or direction not in {-1, 1} or not prices:
        return None
    returns = [Decimal(direction) * (price / entry_price - 1) for price in prices if price > 0]
    if not returns:
        return None
    return {
        "raw_return": returns[-1],
        "maximum_favorable_excursion": max(returns),
        "maximum_adverse_excursion": min(returns),
    }


def a_share_return_decomposition(entry_price: Decimal, direction: int, same_day_close: Decimal | None,
                                 next_day_open: Decimal | None, next_day_close: Decimal | None) -> dict[str, Decimal | None]:
    """Keep intraday, overnight and next-day return legs separate in research settlement."""
    empty = {"trigger_to_close": None, "overnight": None, "next_day_intraday": None, "trigger_to_next_close": None}
    if entry_price <= 0 or direction not in {-1, 1}:
        return empty
    signed = lambda ratio: Decimal(direction) * (ratio - 1)
    return {
        "trigger_to_close": signed(same_day_close / entry_price) if same_day_close and same_day_close > 0 else None,
        "overnight": signed(next_day_open / same_day_close) if same_day_close and same_day_close > 0 and next_day_open and next_day_open > 0 else None,
        "next_day_intraday": signed(next_day_close / next_day_open) if next_day_open and next_day_open > 0 and next_day_close and next_day_close > 0 else None,
        "trigger_to_next_close": signed(next_day_close / entry_price) if next_day_close and next_day_close > 0 else None,
    }


def intraday_outcome_cutoff(as_of_date: date | None) -> datetime:
    """Build an Asia/Shanghai point-in-time cutoff for settlement reads."""
    if as_of_date is None:
        return datetime.now(timezone.utc)
    return datetime.combine(as_of_date, time.max, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)

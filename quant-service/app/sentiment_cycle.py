"""The five short-term sentiment readings, computed from daily bars alone.

The regime the strategies already consult carries breadth, index volume and a
sealed-board count.  Short-term A-share practice reads the tape through five
other numbers, and this session's own data showed they separate outcomes that
breadth does not: across 18,823 limit-up/next-day pairs, a board that opened
three or more times returned +0.43% next open-to-close while one that never
opened returned -0.11%, and a name under 5% turnover returned -0.47% against
+0.85% for one over 30%.  Those are different markets, and nothing in the
current regime distinguishes them.

Everything here derives from ``canonical_bars_daily``: the vendor limit pools
(``limit_list_ths``, ``limit_step``) carry the same facts but start in
2024-08 and are one more contract to keep honest, while the bars are the
source every other part of the system already settles against.

The stage thresholds are the published short-term ones, not values fitted
here.  They are declared as data so a later out-of-sample pass can replace
them with measured ones rather than editing branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .market_rules import LIMIT_TOLERANCE, is_at_limit
#: A board this tall or taller is a "high board" in the ladder reading.
HIGH_BOARD_MIN_HEIGHT = 3
#: How far back a consecutive run is counted; beyond this the height reading
#: stops being about the current cycle.
MAX_LADDER_LOOKBACK = 15


@dataclass(frozen=True)
class SentimentStageThresholds:
    """Published stage boundaries, held as data rather than as branches."""

    label: str
    max_broken_rate: float | None = None
    min_broken_rate: float | None = None
    min_max_height: int | None = None
    max_max_height: int | None = None
    min_promotion_rate: float | None = None
    max_promotion_rate: float | None = None
    min_high_boards: int | None = None
    max_high_boards: int | None = None

    def matches(self, reading: Mapping[str, Any]) -> bool:
        checks = (
            (self.max_broken_rate, reading.get("broken_rate"), lambda a, b: b <= a),
            (self.min_broken_rate, reading.get("broken_rate"), lambda a, b: b >= a),
            (self.min_max_height, reading.get("max_board_height"), lambda a, b: b >= a),
            (self.max_max_height, reading.get("max_board_height"), lambda a, b: b < a),
            (self.min_promotion_rate, reading.get("promotion_rate"), lambda a, b: b >= a),
            (self.max_promotion_rate, reading.get("promotion_rate"), lambda a, b: b < a),
            (self.min_high_boards, reading.get("high_board_count"), lambda a, b: b >= a),
            (self.max_high_boards, reading.get("high_board_count"), lambda a, b: b < a),
        )
        for bound, value, holds in checks:
            if bound is None:
                continue
            if value is None or not holds(bound, value):
                return False
        return True


#: Ordered most extreme first; the first match wins so a tape that reads as
#: both an icepoint and merely weak is reported as the icepoint.
STAGE_THRESHOLDS: tuple[SentimentStageThresholds, ...] = (
    SentimentStageThresholds(
        "climax", max_broken_rate=0.20, min_max_height=7,
        min_promotion_rate=0.60, min_high_boards=5),
    SentimentStageThresholds(
        "icepoint", min_broken_rate=0.50, max_max_height=3,
        max_promotion_rate=0.20, max_high_boards=2),
    SentimentStageThresholds("fermenting", max_broken_rate=0.30, min_promotion_rate=0.40),
    SentimentStageThresholds("ebbing", min_broken_rate=0.40),
)
#: Reported when no threshold set matches, which is most ordinary days.
DEFAULT_STAGE = "mixed"


def _sealed(row: Mapping[str, Any]) -> bool:
    limit_up, close = row.get("limit_up"), row.get("close")
    if limit_up is None or close is None:
        return False
    return is_at_limit(close, limit_up)


def _touched(row: Mapping[str, Any]) -> bool:
    limit_up = row.get("limit_up")
    if limit_up is None:
        return False
    reference = row.get("high") if row.get("high") is not None else row.get("close")
    return reference is not None and is_at_limit(reference, limit_up)


def sealed_symbols(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Names closing locked at their own limit."""
    return {str(row["symbol"]) for row in rows if _sealed(row)}


def broken_symbols(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Names that reached the limit and did not hold it into the close."""
    return {str(row["symbol"]) for row in rows if _touched(row) and not _sealed(row)}


def board_heights(sessions: Sequence[tuple[date, Sequence[Mapping[str, Any]]]]) -> dict[str, int]:
    """Consecutive sealed sessions ending on the last date, per symbol.

    ``sessions`` is ordered oldest first.  A run is broken by any session in
    which the name did not seal, including one it did not trade, so a
    suspension cannot be read as a board that held through it.
    """
    if not sessions:
        return {}
    heights: dict[str, int] = {}
    ordered = list(sessions)[-MAX_LADDER_LOOKBACK:]
    per_session = [sealed_symbols(rows) for _day, rows in ordered]
    for symbol in per_session[-1]:
        height = 0
        for sealed_today in reversed(per_session):
            if symbol not in sealed_today:
                break
            height += 1
        heights[symbol] = height
    return heights


def executable_premium_pct(rows: Iterable[Mapping[str, Any]], symbols: set[str]) -> float | None:
    """Mean open-to-close of yesterday's sealed names, in percent.

    Open-to-close rather than close-to-close on purpose: a name locked at its
    limit cannot be bought at that close, so the close-to-close figure quoted
    by most short-term write-ups is not a return anyone could have taken.
    """
    moves = []
    for row in rows:
        if str(row["symbol"]) not in symbols:
            continue
        open_price, close = row.get("open"), row.get("close")
        if open_price is None or close is None or float(open_price) <= 0:
            continue
        moves.append((float(close) / float(open_price) - 1) * 100)
    return sum(moves) / len(moves) if moves else None


def sentiment_reading(
    sessions: Sequence[tuple[date, Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    """The five readings for the last session in ``sessions``.

    Fields are None rather than zero when the inputs are absent: a day with no
    prior session has no promotion rate, and reporting 0.0 would read as a
    frozen tape instead of an unknown one.
    """
    if not sessions:
        return {"trading_date": None, "stage": DEFAULT_STAGE}
    trading_date, today = sessions[-1]
    sealed_today = sealed_symbols(today)
    broken_today = broken_symbols(today)
    attempted = len(sealed_today) + len(broken_today)
    heights = board_heights(sessions)

    promotion_rate = None
    prior_premium = None
    if len(sessions) >= 2:
        _prior_date, prior_rows = sessions[-2]
        sealed_prior = sealed_symbols(prior_rows)
        if sealed_prior:
            promotion_rate = len(sealed_today & sealed_prior) / len(sealed_prior)
        prior_premium = executable_premium_pct(today, sealed_prior)

    reading = {
        "trading_date": trading_date,
        "sealed_count": len(sealed_today),
        "broken_count": len(broken_today),
        "broken_rate": (len(broken_today) / attempted) if attempted else None,
        "max_board_height": max(heights.values()) if heights else 0,
        "high_board_count": sum(1 for height in heights.values()
                                if height >= HIGH_BOARD_MIN_HEIGHT),
        "promotion_rate": promotion_rate,
        "prior_limit_up_premium_pct": prior_premium,
    }
    reading["stage"] = classify_stage(reading)
    return reading


def classify_stage(reading: Mapping[str, Any]) -> str:
    """Name the cycle stage, or ``mixed`` when no published set matches."""
    for thresholds in STAGE_THRESHOLDS:
        if thresholds.matches(reading):
            return thresholds.label
    return DEFAULT_STAGE


__all__ = [
    "DEFAULT_STAGE", "HIGH_BOARD_MIN_HEIGHT", "LIMIT_TOLERANCE", "MAX_LADDER_LOOKBACK",
    "STAGE_THRESHOLDS", "SentimentStageThresholds", "board_heights", "broken_symbols",
    "classify_stage", "executable_premium_pct", "sealed_symbols", "sentiment_reading",
]

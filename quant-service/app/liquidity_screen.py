"""Shared liquidity/tradability screen for cross-strategy candidates.

No selection module in this codebase filtered on liquidity before this: a
candidate with a tiny 20-day median turnover, a sub-3 yuan price, a brand-new
listing or an ST flag could rank at the top of a strategy's score with no
signal that a real position could not actually be built or exited without
material market impact.  This is a flag, not a silent drop: a candidate that
fails the screen is still recorded (``liquidity_eligible=false`` plus the
specific reasons) so research can see what was excluded and why, rather than
a candidate quietly disappearing from the ledger.

The thresholds below are deliberately conservative, round-number liquidity
floors (not fitted to any strategy's historical returns) chosen only to
exclude the extreme illiquid tail; they are not a claim that stocks just
above the floor are safely tradable at any position size.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

MINIMUM_MEDIAN_DAILY_AMOUNT = 30_000_000  # 20-day median turnover value, yuan
MINIMUM_PRICE = 3.0
MINIMUM_LISTING_AGE_DAYS = 60


def liquidity_eligibility(*, median_daily_amount: float | None, latest_price: float | None,
                          list_date: date | None, as_of_date: date, is_st: bool,
                          is_suspended: bool) -> tuple[bool, list[str]]:
    """Return (eligible, flags). Every failing condition is reported, not just the first."""
    flags: list[str] = []
    if is_suspended:
        flags.append("suspended")
    if is_st:
        flags.append("st_security")
    if median_daily_amount is None:
        flags.append("median_amount_unavailable")
    elif median_daily_amount < MINIMUM_MEDIAN_DAILY_AMOUNT:
        flags.append("median_amount_below_floor")
    if latest_price is None:
        flags.append("price_unavailable")
    elif latest_price < MINIMUM_PRICE:
        flags.append("price_below_floor")
    if list_date is None:
        flags.append("list_date_unavailable")
    elif (as_of_date - list_date) < timedelta(days=MINIMUM_LISTING_AGE_DAYS):
        flags.append("recently_listed")
    return not flags, flags


def median_daily_amount_by_symbol(connection: Any, symbols: list[str], as_of_date: date, *, window_days: int = 20) -> dict[str, float | None]:
    """20-session trailing median traded amount (yuan) per symbol, up to and including as_of_date.

    ``canonical_bars_daily.amount`` is stored in the documented Tushare-compatible
    unit (thousand yuan, see docs/TUSHARE_COMPATIBLE_INGESTION.md and
    daily_bar_repository.py's amount/(volume*close) sanity band of 0.02-0.50);
    it is converted to yuan here so MINIMUM_MEDIAN_DAILY_AMOUNT can stay a
    readable yuan constant instead of every caller needing to know the raw unit.
    """
    if not symbols:
        return {}
    rows = connection.execute(
        """SELECT symbol, percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) * 1000 AS median_amount_yuan
             FROM (
               SELECT symbol, amount, row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                 FROM quant.canonical_bars_daily
                WHERE symbol=ANY(%s) AND trading_date<=%s AND amount IS NOT NULL
             ) recent
            WHERE rn<=%s
            GROUP BY symbol""",
        (symbols, as_of_date, window_days),
    ).fetchall()
    return {str(row["symbol"]): (float(row["median_amount_yuan"]) if row["median_amount_yuan"] is not None else None) for row in rows}


__all__ = ["MINIMUM_LISTING_AGE_DAYS", "MINIMUM_MEDIAN_DAILY_AMOUNT", "MINIMUM_PRICE",
           "liquidity_eligibility", "median_daily_amount_by_symbol"]

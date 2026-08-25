"""Unit coverage for the shared liquidity/tradability screen."""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.liquidity_screen import (
    MINIMUM_LISTING_AGE_DAYS,
    MINIMUM_MEDIAN_DAILY_AMOUNT,
    MINIMUM_PRICE,
    liquidity_eligibility,
    median_daily_amount_by_symbol,
)


class LiquidityScreenTests(unittest.TestCase):
    as_of = date(2026, 8, 25)

    def _base_kwargs(self) -> dict:
        return {
            "median_daily_amount": MINIMUM_MEDIAN_DAILY_AMOUNT * 2, "latest_price": MINIMUM_PRICE + 1.0,
            "list_date": self.as_of - timedelta(days=MINIMUM_LISTING_AGE_DAYS + 30), "as_of_date": self.as_of,
            "is_st": False, "is_suspended": False,
        }

    def test_all_conditions_satisfied_is_eligible_with_no_flags(self) -> None:
        eligible, flags = liquidity_eligibility(**self._base_kwargs())
        self.assertTrue(eligible)
        self.assertEqual(flags, [])

    def test_every_failing_condition_is_reported_not_just_the_first(self) -> None:
        eligible, flags = liquidity_eligibility(
            median_daily_amount=1.0, latest_price=0.5, list_date=self.as_of, as_of_date=self.as_of,
            is_st=True, is_suspended=True,
        )
        self.assertFalse(eligible)
        self.assertEqual(set(flags), {"suspended", "st_security", "median_amount_below_floor",
                                       "price_below_floor", "recently_listed"})

    def test_missing_values_are_flagged_not_silently_passed(self) -> None:
        eligible, flags = liquidity_eligibility(
            median_daily_amount=None, latest_price=None, list_date=None, as_of_date=self.as_of,
            is_st=False, is_suspended=False,
        )
        self.assertFalse(eligible)
        self.assertEqual(set(flags), {"median_amount_unavailable", "price_unavailable", "list_date_unavailable"})

    def test_exactly_at_the_listing_age_floor_is_eligible(self) -> None:
        kwargs = self._base_kwargs()
        kwargs["list_date"] = self.as_of - timedelta(days=MINIMUM_LISTING_AGE_DAYS)
        eligible, flags = liquidity_eligibility(**kwargs)
        self.assertTrue(eligible)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class MedianDailyAmountUnitConversionIntegrationTests(unittest.TestCase):
    """canonical_bars_daily.amount is stored in thousand yuan (docs/TUSHARE_COMPATIBLE_INGESTION.md,
    daily_bar_repository.py's 0.02-0.50 amount/(volume*close) sanity band); a candidate whose
    genuinely-liquid amount was compared without converting to yuan would be flagged illiquid
    for essentially every real A-share symbol."""

    symbol = "999985.SZ"
    as_of_date = date(2099, 1, 20)

    def _cleanup(self) -> None:
        from app.main import db
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_median_amount_is_converted_from_thousand_yuan_to_yuan(self) -> None:
        from app.main import DailyBar, db, upsert_bar
        self._cleanup()
        try:
            # 30,000 thousand-yuan = 30,000,000 yuan: exactly MINIMUM_MEDIAN_DAILY_AMOUNT.
            raw_amount_thousand_yuan = Decimal(MINIMUM_MEDIAN_DAILY_AMOUNT) / Decimal(1000)
            with db.transaction() as connection:
                for offset in range(20):
                    trading_date = self.as_of_date - timedelta(days=offset)
                    upsert_bar(connection, DailyBar(
                        symbol=self.symbol, trading_date=trading_date, open=Decimal("10"), high=Decimal("10.1"),
                        low=Decimal("9.9"), close=Decimal("10"), volume=Decimal("1000000"),
                        amount=raw_amount_thousand_yuan, adj_factor=Decimal("1.0"), is_suspended=False,
                        source="p0-liquidity-unit-test",
                        available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
                    ))
            with db.transaction() as connection:
                result = median_daily_amount_by_symbol(connection, [self.symbol], self.as_of_date)
            self.assertAlmostEqual(result[self.symbol], MINIMUM_MEDIAN_DAILY_AMOUNT, delta=1.0)
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()

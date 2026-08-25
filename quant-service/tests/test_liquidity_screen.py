"""Unit coverage for the shared liquidity/tradability screen."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.liquidity_screen import MINIMUM_LISTING_AGE_DAYS, MINIMUM_MEDIAN_DAILY_AMOUNT, MINIMUM_PRICE, liquidity_eligibility


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


if __name__ == "__main__":
    unittest.main()

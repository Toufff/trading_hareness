from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from decimal import Decimal

from app.market_rules import (
    a_share_limit_prices, a_share_limit_ratio, cn_today, is_at_limit, is_trading_day,
)


class ASahreLimitRatioTests(unittest.TestCase):
    def test_star_market_688_is_20_percent(self):
        self.assertEqual(a_share_limit_ratio("688981.SH"), 0.20)

    def test_star_cdr_689_is_20_percent(self):
        # 689009 (九号公司 CDR) is a STAR-market registration-board name and
        # must not fall through to the 10% mainboard default.
        self.assertEqual(a_share_limit_ratio("689009.SH"), 0.20)

    def test_chinext_300_and_301_are_20_percent(self):
        self.assertEqual(a_share_limit_ratio("300750.SZ"), 0.20)
        self.assertEqual(a_share_limit_ratio("301001.SZ"), 0.20)

    def test_beijing_exchange_old_prefixes_are_30_percent(self):
        self.assertEqual(a_share_limit_ratio("430047.BJ"), 0.30)
        self.assertEqual(a_share_limit_ratio("830799.BJ"), 0.30)

    def test_beijing_exchange_new_92_prefix_is_30_percent(self):
        self.assertEqual(a_share_limit_ratio("920819.BJ"), 0.30)

    def test_mainboard_default_is_10_percent(self):
        self.assertEqual(a_share_limit_ratio("600000.SH"), 0.10)
        self.assertEqual(a_share_limit_ratio("000001.SZ"), 0.10)

    def test_mainboard_st_is_5_percent(self):
        self.assertEqual(a_share_limit_ratio("600001.SH", is_st=True), 0.05)

    def test_star_market_st_is_still_20_percent_not_5(self):
        # Registration-board ST names keep the board's own 20% band; they do
        # not fall back to the mainboard's narrower 5% ST discount.
        self.assertEqual(a_share_limit_ratio("688009.SH", is_st=True), 0.20)
        self.assertEqual(a_share_limit_ratio("300001.SZ", is_st=True), 0.20)

    def test_beijing_st_keeps_30_percent(self):
        self.assertEqual(a_share_limit_ratio("430047.BJ", is_st=True), 0.30)

    def test_accepts_bare_and_prefixed_symbols(self):
        self.assertEqual(a_share_limit_ratio("600000"), 0.10)
        self.assertEqual(a_share_limit_ratio("sh600000"), 0.10)
        self.assertEqual(a_share_limit_ratio("SZ300750"), 0.20)


class IsAtLimitTests(unittest.TestCase):
    def test_exact_match_is_at_limit(self):
        self.assertTrue(is_at_limit(11.00, 11.00))

    def test_within_absolute_tolerance_is_at_limit(self):
        self.assertTrue(is_at_limit(10.996, 11.00))

    def test_hundred_yuan_share_does_not_get_ten_ticks_of_slack(self):
        # A relative *0.999 tolerance would have allowed 99.90 to count as
        # sealed at a 100.00 limit (a full 10 ticks); the absolute tolerance
        # must not.
        self.assertFalse(is_at_limit(99.90, 100.00))
        self.assertTrue(is_at_limit(99.996, 100.00))

    def test_none_inputs_are_not_at_limit(self):
        self.assertFalse(is_at_limit(None, 11.00))
        self.assertFalse(is_at_limit(11.00, None))


class CnTodayTests(unittest.TestCase):
    def test_uses_shanghai_calendar_date_not_utc(self):
        # 2026-01-01 23:30 UTC is already 2026-01-02 07:30 in Shanghai.
        now = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(cn_today(now).isoformat(), "2026-01-02")


class IsTradingDayTests(unittest.TestCase):
    def test_weekday_is_a_trading_day(self):
        self.assertTrue(is_trading_day(date(2026, 9, 1)))  # Tuesday

    def test_weekend_is_not_a_trading_day(self):
        self.assertFalse(is_trading_day(date(2026, 9, 6)))  # Sunday
        self.assertFalse(is_trading_day(date(2026, 9, 5)))  # Saturday


class ASharelimitPriceRoundingTests(unittest.TestCase):
    """Rounding is part of the board rule, and Beijing's is not half-up."""

    def test_beijing_rounds_the_band_inward_one_tick_narrower_than_half_up(self):
        # Measured, not assumed.  Across every Beijing session since
        # 2026-01-01 where the two rules disagree and the day's extreme
        # actually reached a limit, the traded extreme matched the inward
        # value 70/70 up and 8/8 down, and half-up not once.
        # 920002.BJ 2026-09-18: 49.92 * 1.30 = 64.896, * 0.70 = 34.944.
        self.assertEqual(
            a_share_limit_prices("920002.BJ", Decimal("49.92")),
            (Decimal("64.89"), Decimal("34.95")),
        )
        # 920005.BJ same session: 23.12 -> 30.056 / 16.184.
        self.assertEqual(
            a_share_limit_prices("920005.BJ", Decimal("23.12")),
            (Decimal("30.05"), Decimal("16.19")),
        )

    def test_beijing_legacy_4_and_8_prefixes_round_inward_too(self):
        self.assertEqual(
            a_share_limit_prices("430047.BJ", Decimal("49.92"))[0], Decimal("64.89"))
        self.assertEqual(
            a_share_limit_prices("830799.BJ", Decimal("49.92"))[0], Decimal("64.89"))

    def test_shanghai_and_shenzhen_round_half_up_both_ways(self):
        # 9.29 * 1.10 = 10.219 -> 10.22; * 0.90 = 8.361 -> 8.36.
        self.assertEqual(
            a_share_limit_prices("600664.SH", Decimal("9.29")),
            (Decimal("10.22"), Decimal("8.36")),
        )
        # A registration-board name keeps its 20% band.
        self.assertEqual(
            a_share_limit_prices("300750.SZ", Decimal("9.29")),
            (Decimal("11.15"), Decimal("7.43")),
        )

    def test_mainboard_st_name_is_discounted_but_beijing_st_is_not(self):
        self.assertEqual(
            a_share_limit_prices("600001.SH", Decimal("10"), is_st=True)[0], Decimal("10.50"))
        self.assertEqual(
            a_share_limit_prices("920819.BJ", Decimal("10"), is_st=True)[0], Decimal("13.00"))


if __name__ == "__main__":
    unittest.main()

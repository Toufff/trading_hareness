from __future__ import annotations

import unittest

from app.ashare_reality import price_limit_state


class PriceLimitStateTests(unittest.TestCase):
    def test_hundred_yuan_share_does_not_get_relative_slack(self):
        # A relative *0.999 tolerance would have treated 99.90 as sealed
        # against a 100.00 limit (10 ticks of slack); the absolute 0.005
        # tolerance must not.
        quote = {"price": 99.90, "limit_up": 100.00, "symbol": "600000.SH"}
        state = price_limit_state(symbol="600000.SH", quote=quote)
        self.assertFalse(state["at_limit_up"])

    def test_within_absolute_tolerance_is_at_limit_up(self):
        quote = {"price": 99.997, "limit_up": 100.00, "symbol": "600000.SH"}
        state = price_limit_state(symbol="600000.SH", quote=quote)
        self.assertTrue(state["at_limit_up"])

    def test_limit_down_uses_absolute_tolerance_too(self):
        quote = {"price": 90.004, "limit_down": 90.00, "symbol": "600000.SH"}
        state = price_limit_state(symbol="600000.SH", quote=quote)
        self.assertTrue(state["at_limit_down"])

    def test_star_market_st_name_keeps_20_percent_band(self):
        quote = {"is_st": True, "symbol": "688009.SH"}
        state = price_limit_state(symbol="688009.SH", quote=quote)
        self.assertAlmostEqual(state["limit_ratio"], 0.20)

    def test_mainboard_st_name_uses_5_percent_band(self):
        quote = {"is_st": True, "symbol": "600001.SH"}
        state = price_limit_state(symbol="600001.SH", quote=quote)
        self.assertAlmostEqual(state["limit_ratio"], 0.05)


if __name__ == "__main__":
    unittest.main()

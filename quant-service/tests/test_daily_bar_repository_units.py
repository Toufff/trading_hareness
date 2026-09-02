from __future__ import annotations

import unittest
from decimal import Decimal

from app.daily_bar_repository import (
    daily_amount_unit_mismatch,
    shares_to_lots,
    yuan_to_thousand_yuan,
)


class UnitConversionTests(unittest.TestCase):
    def test_shares_to_lots_divides_by_100(self):
        self.assertEqual(shares_to_lots(Decimal("100000")), Decimal("1000"))

    def test_yuan_to_thousand_yuan_divides_by_1000(self):
        self.assertEqual(yuan_to_thousand_yuan(Decimal("1250000")), Decimal("1250"))

    def test_none_passthrough(self):
        self.assertIsNone(shares_to_lots(None))
        self.assertIsNone(yuan_to_thousand_yuan(None))


class DailyAmountUnitGuardAppliesToAllSourcesTests(unittest.TestCase):
    def test_guard_now_catches_a_non_tushare_source_too(self):
        # Before this fix the ratio guard only ran for Tushare's own source
        # keys, so a free-source adapter that forgot to convert its native
        # units (e.g. an unconverted yuan amount) would be promoted silently.
        self.assertTrue(daily_amount_unit_mismatch(
            source="baostock", amount=Decimal("1000000"), volume=Decimal("100"), close=Decimal("100"),
        ))

    def test_guard_still_passes_correctly_converted_free_source_rows(self):
        self.assertFalse(daily_amount_unit_mismatch(
            source="baostock", amount=Decimal("1000"), volume=Decimal("100"), close=Decimal("100"),
        ))

    def test_manual_source_remains_exempt_for_synthetic_fixtures(self):
        self.assertFalse(daily_amount_unit_mismatch(
            source="manual", amount=Decimal("1000000"), volume=Decimal("100"), close=Decimal("100"),
        ))


if __name__ == "__main__":
    unittest.main()

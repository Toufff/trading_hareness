from __future__ import annotations

import unittest
from decimal import Decimal

from app.numeric_utils import decimal_or_none, intraday_number


class DecimalOrNoneTests(unittest.TestCase):
    def test_none_and_empty_string_are_none(self):
        self.assertIsNone(decimal_or_none(None))
        self.assertIsNone(decimal_or_none(""))

    def test_ordinary_values_convert(self):
        self.assertEqual(decimal_or_none("10.5"), Decimal("10.5"))
        self.assertEqual(decimal_or_none(10), Decimal("10"))

    def test_nan_string_does_not_reach_a_numeric_column(self):
        # Decimal("nan") parses without raising, so a naive conversion would
        # silently promote NaN into e.g. daily_fundamentals.
        self.assertIsNone(decimal_or_none("nan"))
        self.assertIsNone(decimal_or_none("NaN"))

    def test_infinity_string_does_not_reach_a_numeric_column(self):
        self.assertIsNone(decimal_or_none("inf"))
        self.assertIsNone(decimal_or_none("-inf"))
        self.assertIsNone(decimal_or_none("Infinity"))

    def test_malformed_value_still_raises(self):
        # Preserve the existing fail-loud contract for genuinely unparsable
        # input; only non-finite results are newly filtered.
        with self.assertRaises(Exception):
            decimal_or_none("not-a-number")


class IntradayNumberTests(unittest.TestCase):
    def test_parses_thousands_separators_and_percent_signs(self):
        self.assertEqual(intraday_number("1,234.5"), 1234.5)
        self.assertEqual(intraday_number("12.3%"), 12.3)

    def test_returns_none_for_unparsable_input(self):
        self.assertIsNone(intraday_number("--"))
        self.assertIsNone(intraday_number(None))


if __name__ == "__main__":
    unittest.main()

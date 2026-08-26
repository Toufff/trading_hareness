"""Unit coverage for reporting-calendar normalization and period selection."""

from __future__ import annotations

import unittest
from datetime import date

from app.earnings_calendar_sync import (
    normalize_disclosure_rows,
    normalize_express_rows,
    normalize_forecast_rows,
    reporting_period,
)


def _parse(value):
    text = str(value or "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8])) if len(text) == 8 and text.isdigit() else None


class ReportingPeriodTests(unittest.TestCase):
    def test_returns_the_most_recent_settled_quarter_end(self):
        self.assertEqual(reporting_period(date(2026, 8, 26)), date(2026, 6, 30))
        self.assertEqual(reporting_period(date(2026, 11, 5)), date(2026, 9, 30))

    def test_a_just_ended_quarter_is_skipped_until_its_calendar_exists(self):
        self.assertEqual(reporting_period(date(2026, 7, 2)), date(2026, 3, 31),
                         "a two-day-old quarter has no registered disclosure calendar yet")
        self.assertEqual(reporting_period(date(2026, 7, 10)), date(2026, 6, 30))

    def test_january_falls_back_to_the_previous_year_end(self):
        self.assertEqual(reporting_period(date(2026, 1, 20)), date(2025, 12, 31))


class NormalizationTests(unittest.TestCase):
    period = date(2026, 6, 30)

    def test_disclosure_rows_keep_scheduled_and_actual_dates(self):
        rows = normalize_disclosure_rows([
            {"ts_code": "600362.SH", "end_date": "20260630", "pre_date": "20260826", "actual_date": "20260826"},
            {"ts_code": "000533.SZ", "end_date": "20260630", "pre_date": "20260820", "actual_date": None},
        ], self.period, _parse)
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(by_symbol["600362.SH"]["pre_date"], date(2026, 8, 26))
        self.assertEqual(by_symbol["600362.SH"]["actual_date"], date(2026, 8, 26))
        self.assertIsNone(by_symbol["000533.SZ"]["actual_date"])

    def test_rows_from_another_period_are_dropped(self):
        rows = normalize_disclosure_rows(
            [{"ts_code": "600362.SH", "end_date": "20260331", "pre_date": "20260426"}], self.period, _parse,
        )
        self.assertEqual(rows, [])

    def test_duplicate_symbols_collapse_to_one_row(self):
        rows = normalize_disclosure_rows([
            {"ts_code": "600362.SH", "end_date": "20260630", "pre_date": "20260820"},
            {"ts_code": "600362.SH", "end_date": "20260630", "pre_date": "20260826"},
        ], self.period, _parse)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pre_date"], date(2026, 8, 26))

    def test_forecast_numbers_are_coerced_from_provider_strings(self):
        rows = normalize_forecast_rows([{
            "ts_code": "600187.SH", "end_date": "20260630", "ann_date": "20260819", "type": "扭亏",
            "p_change_min": "114.4651", "p_change_max": "117.1943", "net_profit_min": "265",
            "last_parent_net": "-1832", "first_ann_date": "20260708", "summary": "预计盈利",
        }], self.period, _parse)
        row = rows[0]
        self.assertEqual(row["forecast_type"], "扭亏")
        self.assertAlmostEqual(row["p_change_min"], 114.4651)
        self.assertEqual(row["last_parent_net"], -1832.0)
        self.assertEqual(row["first_ann_date"], date(2026, 7, 8))
        self.assertIsNone(row["p_change_max"] and None)

    def test_forecast_row_without_an_announcement_date_is_dropped(self):
        self.assertEqual(
            normalize_forecast_rows([{"ts_code": "600187.SH", "end_date": "20260630", "type": "预增"}],
                                    self.period, _parse),
            [],
        )

    def test_express_rows_carry_reported_actuals(self):
        rows = normalize_express_rows([{
            "ts_code": "601231.SH", "end_date": "20260630", "ann_date": "20260710",
            "revenue": 27336366042.06, "n_income": 822095752.12, "diluted_roe": 3.66,
            "yoy_net_profit": 638048458.19,
        }], self.period, _parse)
        self.assertEqual(rows[0]["n_income"], 822095752.12)
        self.assertEqual(rows[0]["diluted_roe"], 3.66)

    def test_unparseable_numbers_become_null_rather_than_raising(self):
        rows = normalize_express_rows([{
            "ts_code": "601231.SH", "end_date": "20260630", "ann_date": "20260710", "revenue": "--",
        }], self.period, _parse)
        self.assertIsNone(rows[0]["revenue"])


if __name__ == "__main__":
    unittest.main()

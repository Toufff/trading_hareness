from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest

from app.analyst_expert_research import (
    _benjamini_hochberg_reject,
    _delisted_stock_return,
    _herding_effective_sample,
    _two_sided_normal_p,
)


class TwoSidedNormalPValueTests(unittest.TestCase):
    def test_none_and_non_finite_are_undefined(self):
        self.assertIsNone(_two_sided_normal_p(None))
        self.assertIsNone(_two_sided_normal_p(float("nan")))
        self.assertIsNone(_two_sided_normal_p(float("inf")))

    def test_larger_t_stat_is_more_significant(self):
        p_small = _two_sided_normal_p(1.0)
        p_large = _two_sided_normal_p(3.0)
        self.assertGreater(p_small, p_large)
        self.assertGreater(p_small, 0.05)
        self.assertLess(p_large, 0.01)


class BenjaminiHochbergRejectTests(unittest.TestCase):
    def test_none_p_values_are_never_rejected(self):
        result = _benjamini_hochberg_reject({"h1": None, "h2": 0.001})
        self.assertFalse(result["h1"])
        self.assertTrue(result["h2"])

    def test_a_single_marginal_p_value_across_many_tests_is_not_rejected(self):
        """The exact failure mode this replaces: one 0.04 among 8 horizons
        used to be enough to declare "go" (t>=1.96 on any one). BH must not
        reject it once diluted across 8 simultaneous tests."""
        p_values = {str(i): 0.04 if i == 0 else 0.6 for i in range(8)}
        result = _benjamini_hochberg_reject(p_values, q=0.05)
        self.assertFalse(any(result.values()), "one marginal p-value among 8 must not survive BH at q=0.05")

    def test_many_small_p_values_are_rejected(self):
        p_values = {str(i): 0.001 for i in range(8)}
        result = _benjamini_hochberg_reject(p_values, q=0.05)
        self.assertTrue(all(result.values()))


class HerdingEffectiveSampleTests(unittest.TestCase):
    def test_perfectly_correlated_analysts_collapse_toward_one_effective_analyst(self):
        rows = [
            {"opinion_date": date(2026, 8, 18), "scope": "stock", "subject_key": "000001.SZ",
             "remote_analyst_id": analyst, "direction": 1}
            for analyst in ("a", "b", "c")
        ]
        result = _herding_effective_sample(rows)
        self.assertEqual(result["analyst_count"], 3)
        self.assertAlmostEqual(result["average_pair_sign_correlation"], 1.0)
        self.assertAlmostEqual(result["effective_independent_analysts"], 1.0)

    def test_independent_analysts_on_different_subjects_are_not_penalized(self):
        rows = [
            {"opinion_date": date(2026, 8, 18), "scope": "stock", "subject_key": f"00000{index}.SZ",
             "remote_analyst_id": f"analyst-{index}", "direction": 1}
            for index in range(4)
        ]
        result = _herding_effective_sample(rows)
        self.assertEqual(result["overlap_pairs"], 0)
        self.assertIsNone(result["average_pair_sign_correlation"])
        self.assertIsNone(result["effective_independent_analysts"])


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class DelistedStockReturnIntegrationTests(unittest.TestCase):
    """``_delisted_stock_return`` must settle a delisted name at its last
    observed close instead of the caller falling back to ``unavailable``,
    which previously erased delisted stocks from every hit_rate denominator.
    """

    symbol = "999990.SZ"
    entry_date = date(2099, 1, 3)
    last_trading_date = date(2099, 1, 4)
    delist_date = date(2099, 1, 4)
    exit_date = date(2099, 1, 5)

    def _cleanup(self) -> None:
        from app.main import db
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_delisted_symbol_settles_at_last_observed_close(self) -> None:
        from app.main import DailyBar, db, upsert_bar

        self._cleanup()
        try:
            with db.transaction() as connection:
                for trading_date, close in ((self.entry_date, Decimal("10.00")), (self.last_trading_date, Decimal("10.50"))):
                    upsert_bar(connection, DailyBar(
                        symbol=self.symbol, trading_date=trading_date, open=close, high=close * Decimal("1.01"),
                        low=close * Decimal("0.99"), close=close, adj_factor=Decimal("1.0"), is_suspended=False,
                        source="p0-delist-test", available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
                    ))
                connection.execute("UPDATE quant.instruments SET delist_date=%s WHERE symbol=%s", (self.delist_date, self.symbol))
                raw_return, basket_size, delisted = _delisted_stock_return(
                    connection, self.symbol, self.entry_date, self.exit_date,
                )
            self.assertTrue(delisted)
            self.assertEqual(basket_size, 1)
            self.assertEqual(Decimal(str(round(raw_return, 6))), Decimal("10.50") / Decimal("10.00") - 1)
        finally:
            self._cleanup()

    def test_not_yet_delisted_as_of_exit_date_is_left_to_the_caller(self) -> None:
        from app.main import DailyBar, db, upsert_bar

        self._cleanup()
        try:
            with db.transaction() as connection:
                upsert_bar(connection, DailyBar(
                    symbol=self.symbol, trading_date=self.entry_date, open=Decimal("10.00"), high=Decimal("10.10"),
                    low=Decimal("9.90"), close=Decimal("10.00"), adj_factor=Decimal("1.0"), is_suspended=False,
                    source="p0-delist-test", available_at=datetime.combine(self.entry_date, datetime.min.time(), tzinfo=timezone.utc),
                ))
                # delist_date is after exit_date: still listed as of exit_date,
                # so this helper must not manufacture a settlement.
                connection.execute(
                    "UPDATE quant.instruments SET delist_date=%s WHERE symbol=%s",
                    (self.exit_date + timedelta(days=30), self.symbol),
                )
                raw_return, basket_size, delisted = _delisted_stock_return(
                    connection, self.symbol, self.entry_date, self.exit_date,
                )
            self.assertFalse(delisted)
            self.assertIsNone(raw_return)
            self.assertEqual(basket_size, 0)
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()

"""The live limit-up anchor is computed locally, not fetched through lxml.

The AKShare/Eastmoney HTML pool this replaces segfaulted the edge collector
at session boundaries five times in one week (lxml htmlParseChunk taking the
GIL on an abandoned worker thread).  These tests pin the local derivation's
semantics and that the crash class cannot quietly return.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone

from app.limit_up_anchor import MAX_ANCHOR_ROWS, live_limit_up_pool_rows


OBSERVED = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)


def _row(symbol, price, high=None, pct=10.0):
    return {"symbol": symbol, "price": price, "pct_change": pct,
            "raw": {"high_price": high if high is not None else price}}


class LiveLimitUpPoolTests(unittest.TestCase):
    limits = {"600127.SH": 10.78, "003040.SZ": 18.73, "300163.SZ": 7.90}
    names = {"600127.SH": "金健米业", "003040.SZ": "楚天龙"}

    def test_a_sealed_board_is_an_anchor(self):
        rows = live_limit_up_pool_rows([_row("600127.SH", 10.78)], self.limits, self.names, OBSERVED)
        self.assertEqual([r["ts_code"] for r in rows], ["600127.SH"])
        self.assertEqual(rows[0]["event_type"], "limit_up_pool")

    def test_a_broken_board_is_not(self):
        # Touched the limit intraday (high) but trades below it now: that is
        # a broken board, and the Eastmoney pool it replaces excluded it too.
        rows = live_limit_up_pool_rows([_row("300163.SZ", 7.57, high=7.90)],
                                       self.limits, self.names, OBSERVED)
        self.assertEqual(rows, [])

    def test_a_name_without_a_limit_price_is_skipped(self):
        rows = live_limit_up_pool_rows([_row("688888.SH", 99.0)], self.limits, self.names, OBSERVED)
        self.assertEqual(rows, [])

    def test_the_title_names_the_stock_and_the_symbol_passes_the_persist_gate(self):
        rows = live_limit_up_pool_rows([_row("600127.SH", 10.78)], self.limits, self.names, OBSERVED)
        self.assertIn("金健米业", rows[0]["title"])
        # persist_market_events drops rows whose ts_code fails this pattern.
        self.assertRegex(rows[0]["ts_code"], r"^\d{6}\.(SH|SZ|BJ)$")
        self.assertEqual(rows[0]["published_at"], OBSERVED.isoformat())

    def test_an_unnamed_symbol_still_anchors_under_its_code(self):
        rows = live_limit_up_pool_rows([_row("300163.SZ", 7.90)], self.limits, None, OBSERVED)
        self.assertIn("300163.SZ", rows[0]["title"])

    def test_the_row_bound_matches_the_replaced_path(self):
        limits = {f"{600000+i}.SH": 10.0 for i in range(MAX_ANCHOR_ROWS + 50)}
        rows = live_limit_up_pool_rows([_row(s, 10.0) for s in limits], limits, None, OBSERVED)
        self.assertEqual(len(rows), MAX_ANCHOR_ROWS)

    def test_evidence_carries_the_derivation_not_a_vendor_page(self):
        rows = live_limit_up_pool_rows([_row("600127.SH", 10.78)], self.limits, self.names, OBSERVED)
        self.assertEqual(rows[0]["raw"]["source"], "fuyao_all_a_plus_stk_limit")
        self.assertIsNone(rows[0]["url"])


class TheIntradayLoopHasNoAkshareLeftTests(unittest.TestCase):
    """AKShare (and its lxml) must stay out of the intraday anchor path."""

    def test_the_refresher_never_calls_akshare(self):
        # The docstring may name AKShare as history; the call path may not.
        from app import main
        source = inspect.getsource(main.refresh_intraday_limit_up_anchors)
        self.assertNotIn("run_akshare", source)
        self.assertNotIn("persist_akshare", source)

    def test_the_eastmoney_pool_helper_is_no_longer_imported_by_main(self):
        from app import main
        self.assertFalse(hasattr(main, "akshare_live_limit_up_pool_events"),
                         "the lxml-backed pool fetch must not be reachable from the intraday loop")


if __name__ == "__main__":
    unittest.main()

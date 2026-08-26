"""Coverage for point-in-time indicator construction from the all-A snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.xiaojie_indicators import (
    MAIN_SECTOR_MIN_LIMIT_UPS,
    PULLBACK_MIN_EXTENSION_PCT,
    board_state,
    candidate_snapshot,
    evaluate_pool,
    leader_pool,
    market_gate_inputs,
    sector_context,
    sector_strength_percentiles,
    session_vwap,
    snapshot_fields,
)


def _row(symbol, price, *, open_=None, high=None, low=None, prev=None,
         volume=1_000_000.0, turnover=None, pct=None):
    open_ = price if open_ is None else open_
    high = price if high is None else high
    low = price if low is None else low
    prev = price if prev is None else prev
    return {
        "symbol": symbol, "price": price, "volume": volume,
        "turnover": turnover if turnover is not None else price * volume,
        "pct_change": pct if pct is not None else (price / prev - 1) * 100,
        "raw": {"open_price": open_, "high_price": high, "low_price": low, "prev_price": prev},
    }


class SnapshotFieldTests(unittest.TestCase):
    def test_ohlc_is_lifted_out_of_the_raw_payload(self):
        fields = snapshot_fields(_row("000001.SZ", 11.0, open_=10.0, high=11.5, low=9.8, prev=10.0))
        self.assertEqual((fields["open"], fields["high"], fields["low"], fields["prev_close"]),
                         (10.0, 11.5, 9.8, 10.0))

    def test_vwap_is_turnover_over_volume(self):
        self.assertEqual(session_vwap({"volume": 1000.0, "turnover": 10500.0}), 10.5)

    def test_vwap_is_absent_without_volume(self):
        self.assertIsNone(session_vwap({"volume": 0.0, "turnover": 10.0}))
        self.assertIsNone(session_vwap({"volume": None, "turnover": None}))


class BoardStateTests(unittest.TestCase):
    def test_sealed_board(self):
        self.assertEqual(board_state({"price": 11.0, "high": 11.0}, 11.0),
                         {"touched": True, "sealed": True, "broken": False})

    def test_a_board_that_broke_is_recognised_from_the_session_high(self):
        # The high is what distinguishes "reached the limit then fell back"
        # from "never got there".
        self.assertEqual(board_state({"price": 10.5, "high": 11.0}, 11.0),
                         {"touched": True, "sealed": False, "broken": True})

    def test_a_name_that_never_reached_the_limit(self):
        self.assertEqual(board_state({"price": 10.0, "high": 10.2}, 11.0),
                         {"touched": False, "sealed": False, "broken": False})

    def test_an_unknown_limit_yields_no_board_state(self):
        self.assertEqual(board_state({"price": 10.0, "high": 10.0}, None),
                         {"touched": False, "sealed": False, "broken": False})


class LeaderPoolTests(unittest.TestCase):
    limits = {"A.SZ": 11.0, "B.SZ": 11.0, "C.SZ": 11.0}

    def test_only_names_at_or_near_the_limit_are_pooled(self):
        rows = [_row("A.SZ", 11.0, high=11.0), _row("B.SZ", 10.8, high=10.9),
                _row("C.SZ", 9.0, high=9.1)]
        self.assertEqual(leader_pool(rows, self.limits), ["A.SZ", "B.SZ"])

    def test_the_bound_truncates_the_weakest_candidates(self):
        rows = [_row("A.SZ", 11.0), _row("B.SZ", 10.9), _row("C.SZ", 10.8)]
        self.assertEqual(leader_pool(rows, self.limits, max_candidates=2), ["A.SZ", "B.SZ"])

    def test_a_broken_board_stays_in_the_pool_via_its_high(self):
        rows = [_row("A.SZ", 10.6, high=11.0)]
        self.assertEqual(leader_pool(rows, self.limits), ["A.SZ"])

    def test_a_symbol_without_a_limit_is_skipped(self):
        self.assertEqual(leader_pool([_row("Z.SZ", 11.0)], self.limits), [])


class MarketGateTests(unittest.TestCase):
    def test_breadth_counts_exclude_unchanged_names(self):
        rows = [_row("A.SZ", 11.0, prev=10.0), _row("B.SZ", 9.0, prev=10.0),
                _row("C.SZ", 10.0, prev=10.0)]
        gate = market_gate_inputs(rows, index_volume_ratio=1.1,
                                  index_above_support=True, main_sector_present=True)
        self.assertEqual((gate["breadth_up_count"], gate["breadth_down_count"]), (1, 1))


class SectorContextTests(unittest.TestCase):
    def test_a_sector_qualifies_on_sealed_boards_not_pool_membership(self):
        rows = {s: _row(s, 11.0 if s != "D.SZ" else 10.7, high=11.0) for s in ("A.SZ", "B.SZ", "C.SZ", "D.SZ")}
        membership = {s: {"S1"} for s in rows}
        limits = {s: 11.0 for s in rows}
        context = sector_context(list(rows), rows, membership, limits)
        self.assertEqual(context["sealed_by_sector"]["S1"], 3)
        self.assertIn("S1", context["main_sectors"])

    def test_a_sector_below_the_floor_is_not_a_main_sector(self):
        rows = {s: _row(s, 11.0, high=11.0) for s in ("A.SZ", "B.SZ")}
        membership = {s: {"S1"} for s in rows}
        context = sector_context(list(rows), rows, membership, {s: 11.0 for s in rows})
        self.assertLess(context["sealed_by_sector"]["S1"], MAIN_SECTOR_MIN_LIMIT_UPS)
        self.assertEqual(context["main_sectors"], set())

    def test_sector_strength_is_cross_sectional_not_pool_relative(self):
        # Every pool name would rank top if strength were computed on the pool.
        rows = {}
        membership = {}
        for index in range(6):
            rows[f"W{index}.SZ"] = _row(f"W{index}.SZ", 10.0, prev=10.0, pct=-3.0)
            membership[f"W{index}.SZ"] = {"WEAK"}
        for index in range(6):
            rows[f"S{index}.SZ"] = _row(f"S{index}.SZ", 11.0, prev=10.0, pct=9.0)
            membership[f"S{index}.SZ"] = {"STRONG"}
        percentiles = sector_strength_percentiles(rows, membership)
        self.assertEqual(percentiles["S0.SZ"], 1.0)
        self.assertEqual(percentiles["W0.SZ"], 0.0)

    def test_thin_sectors_are_excluded_from_the_ranking(self):
        rows = {"A.SZ": _row("A.SZ", 11.0, prev=10.0)}
        self.assertEqual(sector_strength_percentiles(rows, {"A.SZ": {"TINY"}}), {})


class PullbackSemanticTests(unittest.TestCase):
    """A one-word board has price identically equal to VWAP all session."""

    market = {"index_above_support": True, "index_volume_ratio": 1.2,
              "breadth_up_count": 3000, "breadth_down_count": 2000}

    def _snapshot(self, row, **sector_overrides):
        sectors = {"main_sectors": {"S1"}, "ranks": {"A.SZ": 1},
                   "strength_percentile": {"A.SZ": 0.95}, **sector_overrides}
        return candidate_snapshot("A.SZ", row, market=self.market,
                                  reference={"sectors": {"S1"}}, sectors=sectors,
                                  limits={"A.SZ": 11.0})

    def test_a_one_word_board_is_not_a_pullback(self):
        # open == high == low == close, so turnover/volume equals the price.
        row = _row("A.SZ", 11.0, open_=11.0, high=11.0, low=11.0, prev=10.0,
                   volume=1000.0, turnover=11000.0)
        snapshot = self._snapshot(row)
        self.assertEqual(snapshot["_evidence"]["vwap_distance_pct"], 0.0)
        self.assertFalse(snapshot["_evidence"]["extended_above_vwap"])
        self.assertFalse(snapshot["leader_pullback_to_vwap"])

    def test_a_name_sitting_at_its_session_high_is_not_a_pullback(self):
        row = _row("A.SZ", 11.0, open_=10.0, high=11.0, low=10.0, prev=10.0,
                   volume=1000.0, turnover=10300.0)  # vwap 10.30, price at the high
        self.assertFalse(self._snapshot(row)["leader_pullback_to_vwap"])

    def test_a_genuine_retracement_to_vwap_qualifies(self):
        # Ran to 11.0 against a 10.30 VWAP (+6.8%), now back to 10.35 (+0.5%).
        row = _row("A.SZ", 10.35, open_=10.0, high=11.0, low=10.0, prev=10.0,
                   volume=1000.0, turnover=10300.0)
        snapshot = self._snapshot(row)
        self.assertTrue(snapshot["_evidence"]["extended_above_vwap"])
        self.assertTrue(snapshot["leader_pullback_to_vwap"])

    def test_a_break_below_vwap_is_not_a_held_pullback(self):
        row = _row("A.SZ", 10.10, open_=10.0, high=11.0, low=10.0, prev=10.0,
                   volume=1000.0, turnover=10300.0)  # below the 10.30 VWAP
        snapshot = self._snapshot(row)
        self.assertFalse(snapshot["leader_pullback_to_vwap"])
        self.assertFalse(snapshot["support_or_vwap_holds"])

    def test_the_extension_floor_is_declared(self):
        self.assertEqual(PULLBACK_MIN_EXTENSION_PCT, 2.0)


class EvaluatePoolTests(unittest.TestCase):
    def test_an_empty_market_yields_no_candidates_rather_than_raising(self):
        result = evaluate_pool([], limits={}, membership={}, references={},
                               index_volume_ratio=1.0, index_above_support=True,
                               observed_at=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(result["pool_size"], 0)
        self.assertEqual(result["candidates"], [])

    def test_every_evaluation_carries_its_evidence(self):
        rows = [_row(f"S{index}.SZ", 11.0, high=11.0, prev=10.0) for index in range(4)]
        membership = {f"S{index}.SZ": {"S1"} for index in range(4)}
        result = evaluate_pool(rows, limits={f"S{index}.SZ": 11.0 for index in range(4)},
                               membership=membership, references={},
                               index_volume_ratio=1.2, index_above_support=True,
                               observed_at=datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(result["evaluated"], 4)
        for item in result["evaluations"]:
            self.assertIn("board", item["evidence"])
            self.assertIn("decision", item)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(__import__("os").getenv("PGHOST"), "requires the compose PostgreSQL service")
class MarketVolumeBaselineTests(unittest.TestCase):
    """Index series share the daily bar table and carry aggregate volumes."""

    from datetime import date as _date
    trading_date = _date(2099, 6, 1)
    equity, index_symbol = "999974.SZ", "999900.SH"

    def _cleanup(self) -> None:
        from app.main import db
        with db.transaction() as connection:
            for symbol in (self.equity, self.index_symbol):
                connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (symbol,))

    def setUp(self) -> None:
        from datetime import date, datetime, timezone
        from app.main import db
        self._cleanup()
        self.addCleanup(self._cleanup)
        stamp = datetime(2099, 6, 1, tzinfo=timezone.utc)
        with db.transaction() as connection:
            # A listed name has a list_date; an index does not.
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange,list_date) VALUES(%s,'SZ',%s)",
                (self.equity, date(2010, 1, 1)))
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SH')", (self.index_symbol,))
            for symbol, volume in ((self.equity, 1000.0), (self.index_symbol, 9_000_000.0)):
                connection.execute(
                    """INSERT INTO quant.canonical_bars_daily(
                           symbol,trading_date,close,volume,is_suspended,available_at,selected_provider)
                       VALUES(%s,%s,10.0,%s,false,%s,'test')""",
                    (symbol, date(2099, 5, 31), volume, stamp))

    def test_index_aggregate_volume_is_excluded_from_the_baseline(self) -> None:
        from app.main import db
        from app.xiaojie_reference_repository import market_volume_baseline
        with db.transaction() as connection:
            baseline = market_volume_baseline(connection, self.trading_date, sessions=1)
        # Only the listed name counts: 1000 手 -> 100,000 shares.  Including the
        # index would have made this 900,100,000.
        self.assertEqual(baseline, 100_000.0)

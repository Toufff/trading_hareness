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


class Ma5BreakTrackingTests(unittest.TestCase):
    """The MA5 exit rule needs the moment a break started, not just its state."""

    from datetime import datetime as _dt, timezone as _tz
    base = _dt(2026, 8, 26, 5, 0, tzinfo=_tz.utc)

    def test_a_name_above_its_ma5_has_no_running_break(self):
        from app.xiaojie_indicators import track_ma5_break
        state = {}
        result = track_ma5_break(state, "A.SZ", 11.0, 10.0, self.base)
        self.assertEqual(result, {"duration_minutes": 0.0, "recovered": True})
        self.assertEqual(state, {})

    def test_the_break_is_timed_from_its_first_observation(self):
        from datetime import timedelta
        from app.xiaojie_indicators import track_ma5_break
        state = {}
        track_ma5_break(state, "A.SZ", 9.5, 10.0, self.base)
        later = track_ma5_break(state, "A.SZ", 9.4, 10.0, self.base + timedelta(minutes=20))
        self.assertEqual(later["duration_minutes"], 20.0)
        self.assertFalse(later["recovered"])

    def test_recovery_clears_the_timer_and_a_second_break_restarts_it(self):
        from datetime import timedelta
        from app.xiaojie_indicators import track_ma5_break
        state = {}
        track_ma5_break(state, "A.SZ", 9.5, 10.0, self.base)
        track_ma5_break(state, "A.SZ", 10.5, 10.0, self.base + timedelta(minutes=10))
        self.assertEqual(state, {}, "recovering must clear the running break")
        again = track_ma5_break(state, "A.SZ", 9.8, 10.0, self.base + timedelta(minutes=15))
        self.assertEqual(again["duration_minutes"], 0.0,
                         "the second break is timed from itself, not the first")

    def test_a_missing_ma5_yields_no_verdict_rather_than_a_false_one(self):
        from app.xiaojie_indicators import track_ma5_break
        self.assertEqual(track_ma5_break({}, "A.SZ", 10.0, None, self.base),
                         {"duration_minutes": None, "recovered": None})


class NewModeFieldTests(unittest.TestCase):
    """Fields the post-distillation modes gate on."""

    market = {"index_above_support": True, "index_volume_ratio": 1.2,
              "breadth_up_count": 3000, "breadth_down_count": 2000,
              "elapsed_session_minutes": 240, "right_side_confirmed": True, "icepoint": False}

    def _snapshot(self, row, reference=None, sectors=None):
        from app.xiaojie_indicators import candidate_snapshot
        return candidate_snapshot(
            "A.SZ", row, market=self.market,
            reference={"sectors": {"S1"}, **(reference or {})},
            sectors={"main_sectors": {"S1"}, "ranks": {"A.SZ": 1},
                     "strength_percentile": {"A.SZ": 0.95}, "leader_intact": {"A.SZ": True},
                     **(sectors or {})},
            limits={"A.SZ": 11.0})

    def test_breakout_requires_volume_not_just_a_new_high(self):
        row = _row("A.SZ", 10.5, high=10.5, prev=10.0, volume=1_000_000.0)
        drifted = self._snapshot(row, {"high_20d": 10.4, "mean_volume_5d": 2_000_000.0})
        self.assertFalse(drifted["breakout_confirmed"], "clearing a high on thin volume is not a breakout")
        surged = self._snapshot(row, {"high_20d": 10.4, "mean_volume_5d": 500_000.0})
        self.assertTrue(surged["breakout_confirmed"])

    def test_distance_from_ma5_is_a_magnitude_the_rule_can_compare(self):
        # The icepoint rule compares it against a positive floor, so a signed
        # value would demand a name 3% *above* its MA5 for a left-side trial.
        below = self._snapshot(_row("A.SZ", 9.0, prev=10.0), {"ma5": 10.0})
        self.assertAlmostEqual(below["distance_from_ma5_pct"], 10.0, places=5)
        self.assertAlmostEqual(below["_evidence"]["signed_distance_from_ma5_pct"], -10.0, places=5)
        above = self._snapshot(_row("A.SZ", 11.0, prev=10.0), {"ma5": 10.0})
        self.assertAlmostEqual(above["distance_from_ma5_pct"], 10.0, places=5)
        self.assertAlmostEqual(above["_evidence"]["signed_distance_from_ma5_pct"], 10.0, places=5)

    def test_left_side_signal_needs_a_bounce_off_the_low(self):
        at_low = _row("A.SZ", 9.0, low=9.0, prev=10.0)
        self.assertFalse(self._snapshot(at_low, {"ma5": 10.0})["left_side_signal"])
        bounced = _row("A.SZ", 9.2, low=9.0, prev=10.0)
        self.assertTrue(self._snapshot(bounced, {"ma5": 10.0})["left_side_signal"])

    def test_oversold_rebound_needs_a_real_decline_and_a_turn(self):
        falling = _row("A.SZ", 8.0, low=8.0, prev=8.5)
        self.assertFalse(self._snapshot(falling, {"close_10_sessions_ago": 10.0, "low_20d": 7.9})[
            "oversold_rebound_confirmed"], "still falling is not a rebound")
        turning = _row("A.SZ", 8.2, low=7.95, prev=8.0)
        self.assertTrue(self._snapshot(turning, {"close_10_sessions_ago": 10.0, "low_20d": 7.9})[
            "oversold_rebound_confirmed"])

    def test_a_shallow_dip_is_not_oversold(self):
        row = _row("A.SZ", 9.8, low=9.7, prev=9.7)
        self.assertFalse(self._snapshot(row, {"close_10_sessions_ago": 10.0, "low_20d": 9.6})[
            "oversold_rebound_confirmed"])

    def test_the_sector_leader_is_not_its_own_supplement(self):
        row = _row("A.SZ", 10.5, prev=10.0)
        self.assertFalse(self._snapshot(row)["supplement_candidate"], "rank 1 is the leader")
        behind = self._snapshot(row, sectors={"ranks": {"A.SZ": 4}})
        self.assertTrue(behind["supplement_candidate"])

    def test_etf_mode_fails_closed_on_an_equity_cross_section(self):
        # The licensed all-A snapshot carries no funds, so this must be a real
        # False rather than an absent field read as unknown.
        self.assertIs(self._snapshot(_row("A.SZ", 10.0, prev=10.0))["is_etf"], False)

    def test_trend_support_tracks_ma20(self):
        self.assertTrue(self._snapshot(_row("A.SZ", 10.5, prev=10.0), {"ma20": 10.0})["trend_support_holds"])
        self.assertFalse(self._snapshot(_row("A.SZ", 9.5, prev=10.0), {"ma20": 10.0})["trend_support_holds"])


@unittest.skipUnless(__import__("os").getenv("PGHOST"), "requires the compose PostgreSQL service")
class ScanStatusPersistenceTests(unittest.TestCase):
    """source_status is written with the primary signals, before this pass runs.

    Mutating the in-memory dict afterwards never reached the database: the
    strategy executed and wrote its observations and alerts, but every scan
    record showed no trace of it.
    """

    def _scan(self, connection):
        return connection.execute(
            "INSERT INTO quant.intraday_scan_runs(observed_at,status,source_status)"
            " VALUES(now(),'completed','{\"tencent_watch\": {\"status\": \"completed\"}}'::jsonb)"
            " RETURNING scan_id").fetchone()["scan_id"]

    def test_the_status_lands_on_an_already_written_row(self):
        from app.main import db, strategy_json_safe
        from app.xiaojie_observation_repository import persist_scan_status
        with db.transaction() as connection:
            scan_id = self._scan(connection)
            persist_scan_status(connection, scan_id=scan_id,
                                status={"status": "completed", "candidates": 7},
                                json_safe=strategy_json_safe)
            row = connection.execute(
                "SELECT source_status FROM quant.intraday_scan_runs WHERE scan_id=%s",
                (scan_id,)).fetchone()
            connection.execute("DELETE FROM quant.intraday_scan_runs WHERE scan_id=%s", (scan_id,))
        self.assertEqual(row["source_status"]["xiaojie_leader_flow"]["candidates"], 7)

    def test_it_merges_rather_than_replacing_the_primary_status(self):
        from app.main import db, strategy_json_safe
        from app.xiaojie_observation_repository import persist_scan_status
        with db.transaction() as connection:
            scan_id = self._scan(connection)
            persist_scan_status(connection, scan_id=scan_id, status={"status": "completed"},
                                json_safe=strategy_json_safe)
            row = connection.execute(
                "SELECT source_status FROM quant.intraday_scan_runs WHERE scan_id=%s",
                (scan_id,)).fetchone()
            connection.execute("DELETE FROM quant.intraday_scan_runs WHERE scan_id=%s", (scan_id,))
        self.assertEqual(row["source_status"]["tencent_watch"]["status"], "completed",
                         "the primary scan's own status must survive the merge")


@unittest.skipUnless(__import__("os").getenv("PGHOST"), "requires the compose PostgreSQL service")
class AlertBudgetDurabilityTests(unittest.TestCase):
    """The session budget lived in memory, so a deploy handed out a fresh one.

    On 2026-08-27 a 10:32 deploy reset an allowance that had been fully spent
    by 10:27, which makes the cap unenforceable exactly when it binds.
    """

    from datetime import date as _date
    trading_date = _date(2099, 9, 1)
    symbols = ("999951.SZ", "999952.SZ")

    def _cleanup(self):
        from app.main import db
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM quant.xiaojie_leader_flow_observations WHERE trading_date=%s",
                (self.trading_date,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (list(self.symbols),))

    def setUp(self):
        from app.main import db
        self._cleanup()
        self.addCleanup(self._cleanup)
        with db.transaction() as connection:
            for symbol in self.symbols:
                connection.execute(
                    "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT DO NOTHING",
                    (symbol,))

    def test_the_tally_comes_from_the_record_not_from_memory(self):
        from datetime import datetime, timezone
        from app.main import db
        from app.xiaojie_observation_repository import alerted_count, mark_alerted, record_candidates
        stamp = datetime(2099, 9, 1, 2, 0, tzinfo=timezone.utc)
        with db.transaction() as connection:
            self.assertEqual(alerted_count(connection, self.trading_date), 0)
            record_candidates(connection, self.trading_date, stamp, None, [
                {"symbol": symbol, "mode": "reverse_wrap", "decision": "research_candidate",
                 "position": {}, "exit": {}, "risk_flags": [], "reasons": [],
                 "market_gate": {}, "evidence": {}} for symbol in self.symbols])
            self.assertEqual(alerted_count(connection, self.trading_date), 0,
                             "recording a candidate is not the same as alerting on it")
            mark_alerted(connection, self.trading_date, stamp, [(self.symbols[0], "reverse_wrap")])
            self.assertEqual(alerted_count(connection, self.trading_date), 1)

    def test_the_tally_is_scoped_to_its_own_session(self):
        from datetime import datetime, timedelta, timezone
        from app.main import db
        from app.xiaojie_observation_repository import alerted_count, mark_alerted, record_candidates
        stamp = datetime(2099, 9, 1, 2, 0, tzinfo=timezone.utc)
        with db.transaction() as connection:
            record_candidates(connection, self.trading_date, stamp, None, [
                {"symbol": self.symbols[0], "mode": "reverse_wrap", "decision": "research_candidate",
                 "position": {}, "exit": {}, "risk_flags": [], "reasons": [],
                 "market_gate": {}, "evidence": {}}])
            mark_alerted(connection, self.trading_date, stamp, [(self.symbols[0], "reverse_wrap")])
            self.assertEqual(alerted_count(connection, self.trading_date + timedelta(days=1)), 0)


class SealedBoardsAreNotAlertableTests(unittest.TestCase):
    """A locked board cannot be acted on, so it must not spend an alert slot.

    Across 104 observations on 2026-08-27 the 61 found already sealed produced
    0 gains, 57 unchanged and 4 losses from the moment they were flagged; the
    43 found unsealed averaged +0.40%.
    """

    @staticmethod
    def _candidate(symbol, sealed):
        return {"symbol": symbol, "mode": "reverse_wrap",
                "evidence": {"board": {"sealed": sealed, "broken": False, "touched": sealed}}}

    @staticmethod
    def _actionable(candidates):
        return [item for item in candidates
                if not ((item.get("evidence") or {}).get("board") or {}).get("sealed")]

    def test_sealed_candidates_are_filtered_out(self):
        candidates = [self._candidate("A.SZ", True), self._candidate("B.SZ", False)]
        self.assertEqual([item["symbol"] for item in self._actionable(candidates)], ["B.SZ"])

    def test_a_candidate_with_no_board_evidence_is_still_actionable(self):
        self.assertEqual(len(self._actionable([{"symbol": "A.SZ", "mode": "reverse_wrap", "evidence": {}}])), 1)

    def test_an_all_sealed_scan_yields_nothing_to_alert(self):
        self.assertEqual(self._actionable([self._candidate(f"{i}.SZ", True) for i in range(5)]), [])

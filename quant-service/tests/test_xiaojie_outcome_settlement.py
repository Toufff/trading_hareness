"""Real-PostgreSQL coverage for leader-flow outcome settlement.

The point of accumulating observations is to eventually answer whether a mode
works, which needs outcomes attached to them. The distinctions pinned here are
the ones the first live session showed actually matter.
"""

from __future__ import annotations

import datetime as _dt
import os
import unittest
from datetime import date, datetime, timezone

from app.main import db
from app.xiaojie_outcome_settlement import mode_scorecard, settle_session


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class OutcomeSettlementTests(unittest.TestCase):
    session = date(2099, 10, 1)
    next_session = date(2099, 10, 2)
    symbols = ("999941.SZ", "999942.SZ", "999943.SZ")
    stamp = datetime(2099, 10, 1, 2, 0, tzinfo=timezone.utc)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            for table in ("xiaojie_leader_flow_outcomes", "xiaojie_leader_flow_observations"):
                connection.execute(f"DELETE FROM quant.{table} WHERE trading_date=%s", (self.session,))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)", (list(self.symbols),))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (list(self.symbols),))

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        with db.transaction() as connection:
            for symbol in self.symbols:
                connection.execute(
                    "INSERT INTO quant.instruments(symbol,exchange,list_date) VALUES(%s,'SZ',%s)",
                    (symbol, date(2010, 1, 1)))

    def _bar(self, connection, symbol, trading_date, *, open_, close, pre_close, limit_up):
        connection.execute(
            """INSERT INTO quant.canonical_bars_daily(
                   symbol,trading_date,open,high,low,close,pre_close,limit_up,volume,amount,
                   is_suspended,available_at,selected_provider)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,100000,500000,false,%s,'test')""",
            (symbol, trading_date, open_, max(open_, close), min(open_, close), close,
             pre_close, limit_up, self.stamp))

    def _observation(self, connection, symbol, mode, price, sealed, alerted=False):
        connection.execute(
            """INSERT INTO quant.xiaojie_leader_flow_observations(
                   trading_date,symbol,mode,model_version,first_seen_at,last_seen_at,
                   first_scan_id,decision,first_evidence,last_evidence,alerted_at)
               VALUES(%s,%s,%s,'test-v1',%s,%s,NULL,'research_candidate',
                      %s::jsonb,'{}'::jsonb,%s)""",
            (self.session, symbol, mode, self.stamp, self.stamp,
             '{"price": %s, "board": {"sealed": %s}}' % (price, str(sealed).lower()),
             self.stamp if alerted else None))

    def test_session_return_is_measured_from_the_flagged_price(self) -> None:
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=11.0, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            result = settle_session(connection, self.session)
            row = connection.execute(
                "SELECT * FROM quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        self.assertEqual(result["settled"], 1)
        self.assertAlmostEqual(float(row["session_return_pct"]), 10.0, places=4)
        self.assertFalse(row["sealed_at_entry"])

    def test_the_next_session_columns_fill_in_on_a_later_pass(self) -> None:
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=11.0, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            first = settle_session(connection, self.session)
            self.assertEqual(first["pending_next_session"], 1)
            # The following session arrives, and a re-run backfills rather than
            # leaving the forward columns frozen as null.
            self._bar(connection, self.symbols[0], self.next_session,
                      open_=11.5, close=12.0, pre_close=11.0, limit_up=12.1)
            settle_session(connection, self.session)
            row = connection.execute(
                "SELECT * FROM quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        self.assertAlmostEqual(float(row["entry_to_next_close_pct"]), 20.0, places=4)
        self.assertAlmostEqual(float(row["next_open_to_close_pct"]), (12.0 / 11.5 - 1) * 100, places=4)
        self.assertFalse(row["next_open_locked"])

    def test_a_next_open_locked_at_the_limit_is_recorded_as_unbuyable(self) -> None:
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=11.0, pre_close=10.0, limit_up=11.0)
            self._bar(connection, self.symbols[0], self.next_session,
                      open_=12.1, close=12.1, pre_close=11.0, limit_up=12.1)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            settle_session(connection, self.session)
            row = connection.execute(
                "SELECT next_open_locked FROM quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        self.assertTrue(row["next_open_locked"])

    def test_excess_is_measured_against_the_same_session_median(self) -> None:
        with db.transaction() as connection:
            # Two names up 10%, one flat: the cross-sectional median is +10%.
            for symbol, close in zip(self.symbols, (11.0, 11.0, 10.0)):
                self._bar(connection, symbol, self.session,
                          open_=10.0, close=close, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            result = settle_session(connection, self.session)
            row = connection.execute(
                "SELECT excess_session_pct FROM quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        self.assertIsNotNone(result["benchmark_session_pct"])
        # Riding the median earns no excess, which is the point of the column.
        self.assertAlmostEqual(float(row["excess_session_pct"]), 0.0, places=4)

    def test_the_scorecard_excludes_unevaluable_sealed_entries_by_default(self) -> None:
        with db.transaction() as connection:
            for symbol, close in zip(self.symbols, (11.0, 10.0, 10.0)):
                self._bar(connection, symbol, self.session,
                          open_=10.0, close=close, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            self._observation(connection, self.symbols[1], "reverse_wrap", 10.0, True)
            settle_session(connection, self.session)
            excluded = mode_scorecard(connection, self.session, self.session)
            included = mode_scorecard(connection, self.session, self.session, exclude_sealed=False)
        self.assertEqual(excluded[0]["observations"], 1)
        self.assertAlmostEqual(float(excluded[0]["avg_session_pct"]), 10.0, places=4)
        self.assertEqual(included[0]["observations"], 2)
        self.assertAlmostEqual(float(included[0]["avg_session_pct"]), 5.0, places=4,
                               msg="mixing sealed entries in halves the apparent edge")

    def test_returns_are_also_recorded_net_of_a_round_trip(self) -> None:
        from app.ashare_reality import round_trip_cost_pct
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=11.0, pre_close=10.0, limit_up=11.0)
            self._bar(connection, self.symbols[0], self.next_session,
                      open_=11.5, close=12.0, pre_close=11.0, limit_up=12.1)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            settle_session(connection, self.session)
            row = connection.execute(
                "SELECT * FROM quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        cost = float(round_trip_cost_pct())
        self.assertAlmostEqual(float(row["round_trip_cost_pct"]), cost, places=6)
        self.assertAlmostEqual(float(row["net_session_return_pct"]), 10.0 - cost, places=4)
        self.assertAlmostEqual(float(row["net_next_open_to_close_pct"]),
                               (12.0 / 11.5 - 1) * 100 - cost, places=4)

    def test_the_cost_is_charged_once_per_holding_period_not_once_per_column(self) -> None:
        from app.ashare_reality import round_trip_cost_pct
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=11.0, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            settle_session(connection, self.session)
            row = connection.execute(
                "SELECT session_return_pct, net_session_return_pct FROM"
                " quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        drag = float(row["session_return_pct"]) - float(row["net_session_return_pct"])
        self.assertAlmostEqual(drag, float(round_trip_cost_pct()), places=6)

    def test_a_gross_edge_smaller_than_the_round_trip_settles_negative(self) -> None:
        # The case that motivated this: a mode reads as a marginal winner on
        # gross and is a loser to the account that traded it.
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=10.02, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "supplement_rotation", 10.0, False)
            settle_session(connection, self.session)
            row = connection.execute(
                "SELECT session_return_pct, net_session_return_pct FROM"
                " quant.xiaojie_leader_flow_outcomes WHERE symbol=%s",
                (self.symbols[0],)).fetchone()
        self.assertGreater(float(row["session_return_pct"]), 0)
        self.assertLess(float(row["net_session_return_pct"]), 0)

    def test_the_scorecard_counts_a_win_on_net_not_on_gross(self) -> None:
        with db.transaction() as connection:
            # Up 0.02% gross: ahead on the tape, behind after one round trip.
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=10.02, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "supplement_rotation", 10.0, False)
            settle_session(connection, self.session)
            card = mode_scorecard(connection, self.session, self.session)
        self.assertEqual(float(card[0]["session_win_pct"]), 0.0)
        self.assertGreater(float(card[0]["avg_session_pct"]), 0)
        self.assertLess(float(card[0]["avg_net_session_pct"]), 0)

    def test_settling_twice_refreshes_rather_than_duplicating(self) -> None:
        with db.transaction() as connection:
            self._bar(connection, self.symbols[0], self.session,
                      open_=10.0, close=11.0, pre_close=10.0, limit_up=11.0)
            self._observation(connection, self.symbols[0], "reverse_wrap", 10.0, False)
            settle_session(connection, self.session)
            settle_session(connection, self.session)
            count = connection.execute(
                "SELECT count(*) n FROM quant.xiaojie_leader_flow_outcomes WHERE trading_date=%s",
                (self.session,)).fetchone()
        self.assertEqual(count["n"], 1)



class ObservationTransferContractTests(unittest.TestCase):
    """The strategy runs on the edge; settlement runs on the workstation.

    Without a transfer entry the observations accumulate where nothing analyses
    them, which would have made a week of accumulation produce an empty record.
    """

    def test_observations_are_carried_by_the_evidence_transfer(self):
        from app.edge_evidence_transfer import TRANSFER_TABLES
        table = next((item for item in TRANSFER_TABLES
                      if item.name == "xiaojie_leader_flow_observations"), None)
        self.assertIsNotNone(table, "leader-flow observations must reach the workstation")
        self.assertEqual(table.conflict_columns, ("trading_date", "symbol", "mode"))

    def test_the_watermark_advances_when_a_held_setup_is_re_observed(self):
        from app.edge_evidence_transfer import TRANSFER_TABLES
        table = next(item for item in TRANSFER_TABLES
                     if item.name == "xiaojie_leader_flow_observations")
        # first_seen_at never changes after insert, so it would strand every
        # update; last_seen_at moves on each re-observation.
        self.assertEqual(table.watermark_column, "last_seen_at")

    def test_evidence_columns_are_declared_as_json(self):
        from app.edge_evidence_transfer import TRANSFER_TABLES
        table = next(item for item in TRANSFER_TABLES
                     if item.name == "xiaojie_leader_flow_observations")
        for column in ("first_evidence", "last_evidence", "risk_flags", "market_gate"):
            self.assertIn(column, table.json_columns)

if __name__ == "__main__":
    unittest.main()

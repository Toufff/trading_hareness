"""The launch radar warns before the board, without drowning in the base rate.

Crossing +5% preceded a seal by a median 15 minutes, but only 10-14% of
crossers sealed at all (2026-08-26..28), so admission demands volume burst,
a standing sector anchor and velocity together - and stays research-only.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone

from app.launch_radar import (
    LAUNCH_VELOCITY_WINDOW_SECONDS, MAX_LAUNCH_ROWS, evaluate_launch_radar, track_velocity,
)

T0 = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)


def _row(symbol, price, pct, volume=8_000_000, high=None):
    return {"symbol": symbol, "price": price, "pct_change": pct, "volume": volume,
            "raw": {"high_price": high if high is not None else price, "low_price": price * 0.97}}


def _radar(rows, *, state=None, limits=None, membership=None, references=None, pool=(),
           observed_at=T0, elapsed=120):
    return evaluate_launch_radar(
        rows, limits=limits or {}, membership=membership or {},
        references=references or {}, pool=list(pool),
        velocity_state=state if state is not None else {},
        observed_at=observed_at, elapsed_session_minutes=elapsed)


class VelocityTrackerTests(unittest.TestCase):
    def test_the_first_sighting_is_not_a_speed(self):
        self.assertIsNone(track_velocity({}, "A", 10.0, T0))

    def test_speed_is_measured_against_the_window_start(self):
        state = {}
        track_velocity(state, "A", 10.0, T0)
        speed = track_velocity(state, "A", 10.2, T0 + timedelta(seconds=60))
        self.assertAlmostEqual(speed, 2.0, places=6)

    def test_stale_history_is_pruned_rather_than_compared_against(self):
        state = {}
        track_velocity(state, "A", 5.0, T0 - timedelta(seconds=LAUNCH_VELOCITY_WINDOW_SECONDS + 60))
        self.assertIsNone(track_velocity(state, "A", 10.0, T0),
                          "a price from before the window must not fabricate a 100% velocity")


class AdmissionTests(unittest.TestCase):
    """All three gates, together, inside the band."""

    LIMITS = {"A.SZ": 11.0, "LEAD.SZ": 22.0}
    MEMBERSHIP = {"A.SZ": {"S1"}, "LEAD.SZ": {"S1"}}
    REFERENCES = {"A.SZ": {"mean_volume_5d": 2_000_000.0}}

    def _launch(self, *, price=10.6, pct=6.0, volume=8_000_000, membership=None,
                references=None, two_scans=True):
        state = {}
        rows = [_row("A.SZ", price - 0.25, pct - 2.4, volume=volume // 2),
                _row("LEAD.SZ", 22.0, 10.0)]
        if two_scans:
            _radar(rows, state=state, limits=self.LIMITS,
                   membership=membership or self.MEMBERSHIP,
                   references=references or self.REFERENCES, pool=["LEAD.SZ"],
                   observed_at=T0 - timedelta(seconds=90))
        rows2 = [_row("A.SZ", price, pct, volume=volume), _row("LEAD.SZ", 22.0, 10.0)]
        return _radar(rows2, state=state, limits=self.LIMITS,
                      membership=membership or self.MEMBERSHIP,
                      references=references or self.REFERENCES, pool=["LEAD.SZ"])

    def test_a_bursting_anchored_moving_name_is_admitted(self):
        result = self._launch()
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["A.SZ"])
        evidence = result["candidates"][0]["evidence"]
        self.assertGreaterEqual(evidence["velocity_pct"], 1.5)
        self.assertGreaterEqual(evidence["volume_ratio"], 2.0)
        self.assertEqual(evidence["anchor_sectors"], {"S1": 1})

    def test_no_anchor_no_admission(self):
        result = self._launch(membership={"A.SZ": {"S2"}, "LEAD.SZ": {"S1"}})
        self.assertEqual(result["candidates"], [])

    def test_no_volume_burst_no_admission(self):
        result = self._launch(references={"A.SZ": {"mean_volume_5d": 40_000_000.0}})
        self.assertEqual(result["candidates"], [])

    def test_a_single_sighting_has_no_velocity_and_is_not_admitted(self):
        result = self._launch(two_scans=False)
        self.assertEqual(result["candidates"], [])

    def test_below_the_band_is_ignored(self):
        result = self._launch(pct=4.0, price=10.4)
        self.assertEqual(result["candidates"], [])

    def test_leader_pool_territory_is_not_a_launch(self):
        # Within NEAR_LIMIT_PCT of the limit: the leader pool's job already.
        result = self._launch(price=10.85, pct=8.5)
        self.assertEqual(result["candidates"], [])

    def test_evidence_is_settleable(self):
        evidence = self._launch()["candidates"][0]["evidence"]
        self.assertIn("price", evidence)
        self.assertFalse(evidence["board"]["sealed"],
                         "a launch is pre-board by construction, so it is always evaluable")


class TruncationTests(unittest.TestCase):
    def test_truncation_drops_the_slowest_and_is_reported(self):
        limits = {f"{i:06d}.SZ": 11.0 for i in range(MAX_LAUNCH_ROWS + 10)}
        limits["LEAD.SZ"] = 22.0
        membership = {s: {"S1"} for s in limits}
        references = {s: {"mean_volume_5d": 1_000_000.0} for s in limits}
        state = {}
        def rows_at(base):
            out = [_row("LEAD.SZ", 22.0, 10.0)]
            for index, symbol in enumerate(sorted(set(limits) - {"LEAD.SZ"})):
                out.append(_row(symbol, base + index * 0.0001, 6.0, volume=6_000_000))
            return out
        _radar(rows_at(10.3), state=state, limits=limits, membership=membership,
               references=references, pool=["LEAD.SZ"], observed_at=T0 - timedelta(seconds=90))
        result = _radar(rows_at(10.6), state=state, limits=limits, membership=membership,
                        references=references, pool=["LEAD.SZ"])
        self.assertEqual(len(result["candidates"]), MAX_LAUNCH_ROWS)
        self.assertEqual(result["truncated"], 10)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class LaunchObservationsSettleTests(unittest.TestCase):
    """Launch sightings ride the shared outcomes path with no schema of their own."""

    session = date(2099, 11, 2)
    symbol = "999951.SZ"

    def _cleanup(self):
        from app.main import db
        with db.transaction() as c:
            for table in ("xiaojie_leader_flow_outcomes", "xiaojie_leader_flow_observations"):
                c.execute(f"DELETE FROM quant.{table} WHERE trading_date=%s", (self.session,))
            c.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            c.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_a_launch_sighting_settles_and_scores_under_its_own_mode(self):
        from app.launch_radar import record_launch_observations
        from app.main import db
        from app.xiaojie_outcome_settlement import mode_scorecard, settle_session
        self._cleanup()
        self.addCleanup(self._cleanup)
        stamp = datetime(2099, 11, 2, 2, 0, tzinfo=timezone.utc)
        with db.transaction() as c:
            c.execute("INSERT INTO quant.instruments(symbol,exchange,list_date) VALUES(%s,'SZ','2010-01-01')",
                      (self.symbol,))
            c.execute("""INSERT INTO quant.canonical_bars_daily(
                             symbol,trading_date,open,high,low,close,pre_close,limit_up,volume,amount,
                             is_suspended,available_at,selected_provider)
                         VALUES(%s,%s,10.0,11.0,10.0,11.0,10.0,11.0,100000,500000,false,%s,'test')""",
                      (self.symbol, self.session, stamp))
            fresh = record_launch_observations(c, self.session, stamp, None, [{
                "symbol": self.symbol, "decision": "launch_watch",
                "evidence": {"price": 10.55, "board": {"sealed": False}}}])
            self.assertEqual(fresh, 1)
            settle_session(c, self.session)
            rows = mode_scorecard(c, self.session, self.session)
        modes = {row["mode"]: row for row in rows}
        self.assertIn("launch_radar", modes)
        # Flagged at 10.55, closed at the 11.0 limit: the radar's promise.
        self.assertAlmostEqual(float(modes["launch_radar"]["avg_session_pct"]), 4.2654, places=3)


if __name__ == "__main__":
    unittest.main()


class WarmupBandTests(unittest.TestCase):
    """Velocity must be warm the moment a name crosses into the band."""

    def test_below_the_warmup_floor_nothing_is_tracked(self):
        state = {}
        _radar([_row("A.SZ", 10.2, 2.0)], state=state, limits={"A.SZ": 11.0})
        self.assertNotIn("A.SZ", state, "a +2% drift is noise, not a warming launch")

    def test_a_warm_zone_sighting_arms_the_crossing_scan(self):
        # First seen at +3.6% (warm zone), crosses to +6% one scan later:
        # admitted immediately - the crossing IS the launch moment.
        state = {}
        limits = {"A.SZ": 11.0, "LEAD.SZ": 22.0}
        membership = {"A.SZ": {"S1"}, "LEAD.SZ": {"S1"}}
        references = {"A.SZ": {"mean_volume_5d": 2_000_000.0}}
        _radar([_row("A.SZ", 10.35, 3.6, volume=4_000_000), _row("LEAD.SZ", 22.0, 10.0)],
               state=state, limits=limits, membership=membership, references=references,
               pool=["LEAD.SZ"], observed_at=T0 - timedelta(seconds=90))
        result = _radar([_row("A.SZ", 10.6, 6.0, volume=8_000_000), _row("LEAD.SZ", 22.0, 10.0)],
                        state=state, limits=limits, membership=membership,
                        references=references, pool=["LEAD.SZ"])
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["A.SZ"])

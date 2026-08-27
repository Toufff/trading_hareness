"""Regressions for defects found reviewing xiaojie-leader-flow-v1.

Kept separate from the module's own suite so the two can be edited
independently. Each test names the concrete failure it locks out.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app.xiaojie_leader_flow import (
    DEFAULT_PARAMETERS, EXIT_SEVERITY, MODE_ALERT_PRIORITY, alert_priority, evaluate_snapshot,
)


GATE = {
    "index_above_support": True, "index_volume_ratio": 1.25,
    "breadth_up_count": 3200, "breadth_down_count": 1200,
    "main_sector_present": True, "sector_strength_percentile": 0.92,
    "candidate_strength_rank": 1,
}


def _exit(**extra) -> dict:
    return evaluate_snapshot({**GATE, "leader_pullback_to_vwap": True, **extra})["exit"]


class ExitSeverityIsNeverDowngradedTests(unittest.TestCase):
    """Sequential assignment let a milder later rule overwrite a stronger one.

    A broken support said ``exit`` on its own, but the same snapshot that had
    additionally gone three days without a new high came back ``reduce_or_exit``
    - accumulating bearish evidence produced a weaker recommendation.
    """

    def test_support_break_alone_exits(self):
        self.assertEqual(_exit(box_support_broken=True)["action"], "exit")

    def test_support_break_is_not_softened_by_a_milder_rule(self):
        result = _exit(box_support_broken=True, days_without_new_high=3)
        self.assertEqual(result["action"], "exit")
        self.assertEqual(result["codes"], ["support_break", "no_new_high_3d"])

    def test_support_break_is_not_softened_by_a_fading_sector(self):
        self.assertEqual(
            _exit(box_support_broken=True, limit_up_break=True, sector_strength_fades=True)["action"],
            "exit",
        )

    def test_every_bearish_condition_at_once_is_the_strongest_action(self):
        result = _exit(box_support_broken=True, days_without_new_high=3, days_without_rise=5,
                       limit_up_break=True, sector_strength_fades=True,
                       ma5_break_duration_minutes=30, ma5_recovered=False)
        self.assertEqual(result["action"], "exit")
        self.assertEqual(len(result["codes"]), 5)

    def test_a_mild_rule_alone_still_reports_its_own_action(self):
        self.assertEqual(_exit(days_without_new_high=3)["action"], "reduce_or_exit")
        self.assertEqual(_exit(ma5_break_duration_minutes=30, ma5_recovered=False)["action"], "reduce_half")

    def test_no_condition_holds(self):
        self.assertEqual(_exit()["action"], "hold_or_wait")
        self.assertEqual(_exit()["codes"], [])

    def test_severity_order_is_total_and_ascending(self):
        self.assertEqual(
            sorted(EXIT_SEVERITY, key=EXIT_SEVERITY.get),
            ["hold_or_wait", "reduce_half", "reduce_or_exit", "exit"],
        )


class StrictBooleanContractTests(unittest.TestCase):
    """Mode selection used Python truthiness, so a serialized ``"false"`` read as true."""

    ONE_WORD = {"prior_one_word_board": True, "limit_up_return_flow": True, "re_seal_confirmed": True}

    def test_real_booleans_still_select_the_mode(self):
        result = evaluate_snapshot({**GATE, **self.ONE_WORD})
        self.assertEqual(result["mode"], "one_word_return_flow")
        self.assertEqual(result["decision"], "research_candidate")

    def test_the_string_false_no_longer_produces_a_high_risk_candidate(self):
        result = evaluate_snapshot({**GATE, "prior_one_word_board": "false",
                                    "limit_up_return_flow": "false", "re_seal_confirmed": "false"})
        self.assertIsNone(result["mode"])
        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["position"]["target_fraction"], 0.0)

    def test_truthy_non_booleans_are_treated_as_absent(self):
        for value in ("true", 1, "yes", [1]):
            with self.subTest(value=value):
                result = evaluate_snapshot({**GATE, "prior_one_word_board": value,
                                            "limit_up_return_flow": value, "re_seal_confirmed": value})
                self.assertIsNone(result["mode"])

    def test_a_string_flag_cannot_bypass_the_back_row_block(self):
        # "false" must not disable the block, and it must not enable it either.
        blocked = evaluate_snapshot({**GATE, "leader_pullback_to_vwap": True, "is_back_row": True})
        self.assertEqual(blocked["decision"], "no_trade")
        self.assertIn("back_row_no_chase", blocked["risk_flags"])
        loose = evaluate_snapshot({**GATE, "leader_pullback_to_vwap": True, "is_back_row": "false"})
        self.assertEqual(loose["decision"], "research_candidate")


class PreregisteredThresholdTests(unittest.TestCase):
    """The three most overfit-prone numbers were pinned in code, beyond calibration."""

    def test_the_previously_hardcoded_thresholds_are_registered(self):
        for name, value in (("leader_rank_max", 2), ("days_without_new_high_min", 3),
                            ("days_without_rise_min", 5)):
            with self.subTest(name=name):
                self.assertEqual(DEFAULT_PARAMETERS[name], value)

    def test_leader_rank_is_calibratable(self):
        snapshot = {**GATE, "candidate_strength_rank": 3, "leader_pullback_to_vwap": True}
        self.assertEqual(evaluate_snapshot(snapshot)["decision"], "no_trade")
        widened = evaluate_snapshot(snapshot, {"leader_rank_max": 3})
        self.assertEqual(widened["decision"], "research_candidate")

    def test_time_stops_are_calibratable(self):
        self.assertEqual(_exit(days_without_rise=4)["codes"], [])
        loosened = evaluate_snapshot(
            {**GATE, "leader_pullback_to_vwap": True, "days_without_rise": 4},
            {"days_without_rise_min": 4},
        )
        self.assertIn("time_stop_5d", loosened["exit"]["codes"])

    def test_the_returned_parameters_reflect_the_override(self):
        result = evaluate_snapshot({**GATE, "leader_pullback_to_vwap": True}, {"leader_rank_max": 4})
        self.assertEqual(result["parameters"]["leader_rank_max"], 4)

    def test_an_unregistered_parameter_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            evaluate_snapshot(GATE, {"invented_knob": 1})



class SupplementRotationReachabilityTests(unittest.TestCase):
    """A supplement is not the leader, so gating it on rank 1-2 made it dead.

    The live indicator marks a name as a supplement candidate only at rank 3 or
    worse, while the leader gate required rank 1-2: the two intervals never
    intersected, so the mode could be selected but never produce a candidate.
    """

    base = {**GATE, "candidate_strength_rank": 4, "is_back_row": True,
            "supplement_candidate": True, "leader_not_broken": True}

    def test_a_back_row_supplement_can_now_be_a_candidate(self):
        result = evaluate_snapshot(self.base)
        self.assertEqual(result["mode"], "supplement_rotation")
        self.assertEqual(result["decision"], "research_candidate")

    def test_it_stays_capped_at_the_small_fraction(self):
        result = evaluate_snapshot(self.base)
        self.assertLessEqual(result["position"]["target_fraction"], 0.10)

    def test_the_exemption_does_not_leak_to_other_modes(self):
        # A back-row name with no supplement premise is still blocked.
        result = evaluate_snapshot({**GATE, "candidate_strength_rank": 4, "is_back_row": True,
                                    "leader_pullback_to_vwap": True})
        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("back_row_no_chase", result["risk_flags"])

    def test_a_supplement_whose_leader_broke_is_still_blocked(self):
        result = evaluate_snapshot({**self.base, "leader_not_broken": False})
        self.assertEqual(result["decision"], "no_trade")


class AlertPriorityTests(unittest.TestCase):
    """Scarce alert slots must go by conviction, not by which mode is largest.

    On 2026-08-26's close 43 of 72 candidates were supplement rotations at a 5%
    research position; unordered, they would have consumed the daily alert
    budget ahead of every 20% leader setup.
    """

    @staticmethod
    def _candidate(mode, rank=1):
        return {"symbol": f"{mode[:4]}.SZ", "mode": mode,
                "evidence": {"candidate_strength_rank": rank}}

    def test_the_core_leader_setup_outranks_a_supplement(self):
        ordered = sorted([self._candidate("supplement_rotation"),
                          self._candidate("leader_pullback")], key=alert_priority)
        self.assertEqual([item["mode"] for item in ordered],
                         ["leader_pullback", "supplement_rotation"])

    def test_supplements_and_left_side_trials_sort_last(self):
        modes = ["supplement_rotation", "icepoint_left_trial", "leader_pullback",
                 "reverse_wrap", "one_word_return_flow"]
        ordered = [item["mode"] for item in
                   sorted([self._candidate(mode) for mode in modes], key=alert_priority)]
        self.assertEqual(ordered[:3], ["leader_pullback", "one_word_return_flow", "reverse_wrap"])
        self.assertEqual(ordered[-1], "supplement_rotation")

    def test_a_crowd_of_supplements_cannot_bury_one_leader_setup(self):
        crowd = [self._candidate("supplement_rotation", rank=index) for index in range(3, 43)]
        crowd.append(self._candidate("leader_pullback"))
        self.assertEqual(sorted(crowd, key=alert_priority)[0]["mode"], "leader_pullback")

    def test_ties_inside_a_mode_break_on_sector_rank(self):
        ordered = sorted([self._candidate("reverse_wrap", rank=4),
                          self._candidate("reverse_wrap", rank=1)], key=alert_priority)
        self.assertEqual([item["evidence"]["candidate_strength_rank"] for item in ordered], [1, 4])

    def test_an_unknown_mode_sorts_after_every_declared_one(self):
        ordered = sorted([self._candidate("something_new"),
                          self._candidate("supplement_rotation")], key=alert_priority)
        self.assertEqual(ordered[-1]["mode"], "something_new")

    def test_a_missing_rank_does_not_win_the_tie(self):
        without = {"symbol": "A.SZ", "mode": "reverse_wrap", "evidence": {}}
        ordered = sorted([without, self._candidate("reverse_wrap", rank=2)], key=alert_priority)
        self.assertEqual(ordered[0]["evidence"].get("candidate_strength_rank"), 2)

    def test_every_declared_mode_has_a_distinct_priority(self):
        self.assertEqual(len(set(MODE_ALERT_PRIORITY.values())), len(MODE_ALERT_PRIORITY))

if __name__ == "__main__":
    unittest.main()


class AlertNamesTheStockNotJustItsCodeTests(unittest.TestCase):
    """An alert is read by a person, so it has to say which stock it is.

    The live snapshot the strategy runs on carries no name, so every alert
    identified its subject by ts_code alone - unambiguous to the pipeline and
    unreadable on a phone. Names ride along with the per-session reference,
    which is cached per trading date.
    """

    names = {"600127.SH": "金健米业"}

    def _candidate(self, symbol="600127.SH"):
        return {"symbol": symbol, "mode": "right_side_breakout",
                "position": {"target_fraction": 0.05}, "risk_flags": [],
                "evidence": {"pct_change": 10.0, "board": {"sealed": True}}}

    def _text(self, candidate, names):
        from app.main import _xiaojie_alert_text
        return _xiaojie_alert_text(candidate, date(2026, 8, 27), names)

    def test_the_chinese_name_appears_alongside_the_code(self):
        text = self._text(self._candidate(), self.names)
        self.assertIn("金健米业", text)
        self.assertIn("600127.SH", text)

    def test_the_name_leads_so_a_notification_reads_as_a_stock(self):
        text = self._text(self._candidate(), self.names)
        self.assertLess(text.index("金健米业"), text.index("600127.SH"))

    def test_an_unnamed_symbol_still_alerts_on_its_code(self):
        # A fresh listing that has not reached ``instruments`` must not lose
        # its alert over a missing label.
        text = self._text(self._candidate("301999.SZ"), self.names)
        self.assertIn("301999.SZ", text)

    def test_no_names_at_all_degrades_rather_than_raising(self):
        self.assertIn("600127.SH", self._text(self._candidate(), None))

    def test_the_body_of_the_alert_is_unchanged(self):
        text = self._text(self._candidate(), self.names)
        for fragment in ("【研究观察·小杰龙头】", "封板", "涨幅 10.00%",
                         "研究仓位参考", "不构成交易指令"):
            self.assertIn(fragment, text)


class SessionReferenceCarriesNamesTests(unittest.TestCase):
    """The alert can only name a stock if the session reference loaded names."""

    def test_the_loader_reads_names_once_per_session(self):
        from app.xiaojie_reference_repository import instrument_names
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "600127.SH", "name": "金健米业"},
            {"symbol": "000017.SZ", "name": "深中华A"},
        ]
        self.assertEqual(instrument_names(connection),
                         {"600127.SH": "金健米业", "000017.SZ": "深中华A"})
        self.assertEqual(connection.execute.call_count, 1)

    def test_the_session_reference_exposes_them_under_names(self):
        from app.xiaojie_reference_repository import load_session_reference
        with patch("app.xiaojie_reference_repository.trade_limits", return_value={}), \
             patch("app.xiaojie_reference_repository.sector_membership", return_value={}), \
             patch("app.xiaojie_reference_repository.candidate_references", return_value={}), \
             patch("app.xiaojie_reference_repository.market_volume_baseline", return_value=None), \
             patch("app.xiaojie_reference_repository.instrument_names",
                   return_value={"600127.SH": "金健米业"}):
            reference = load_session_reference(MagicMock(), date(2026, 8, 27))
        self.assertEqual(reference["names"], {"600127.SH": "金健米业"})

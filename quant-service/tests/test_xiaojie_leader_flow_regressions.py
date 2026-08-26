"""Regressions for defects found reviewing xiaojie-leader-flow-v1.

Kept separate from the module's own suite so the two can be edited
independently. Each test names the concrete failure it locks out.
"""

from __future__ import annotations

import unittest

from app.xiaojie_leader_flow import DEFAULT_PARAMETERS, EXIT_SEVERITY, evaluate_snapshot


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

if __name__ == "__main__":
    unittest.main()

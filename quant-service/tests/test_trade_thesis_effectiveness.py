import unittest

from app.trade_thesis.effectiveness import collect_three_arm_snapshot, evaluate_three_arms


def bars():
    return [
        {"date": "2026-09-16", "open": 10, "high": 11, "low": 9.8, "close": 10.5, "pre_close": 9.9,
         "limit_up": 11, "limit_down": 9, "adj_factor": 1, "is_suspended": False},
        {"date": "2026-09-17", "open": 10.5, "high": 11, "low": 10.2, "close": 10.8, "pre_close": 10.5,
         "limit_up": 11.5, "limit_down": 9.5, "adj_factor": 1, "is_suspended": False},
    ]


class ThreeArmEffectivenessTests(unittest.TestCase):
    POLICY = {"status": "approved", "policy_hash": "a" * 64}

    def test_snapshot_freezes_existing_selection_ranking_and_research_projection(self):
        scan = {"accumulation": {"selected": [{"symbol": "000001.SZ", "rank": 1, "rank_score": 80,
                                                   "origin_id": "o1", "eligibility": "eligible", "action": "recommend"}],
                                 "observation_list": [{"symbol": "000002.SZ", "rank": 2}]}}
        receipt = {"source_run_id": "run-1", "items": [{"symbol": "000001.SZ", "thesis_id": "t1", "evaluation_id": "e1",
                    "states": {"entry_state": "eligible"}, "current_rankings": [{"lane": "breakout", "rank": 10}]}]}
        snapshot = collect_three_arm_snapshot(scan, receipt, captured_at="2026-09-20T12:00:00+00:00")
        self.assertTrue(snapshot["arms"]["current_baseline"][0]["selected"])
        self.assertEqual(snapshot["arms"]["pure_machine"][0]["rankings"][0]["rank"], 1)
        lifecycle = snapshot["arms"]["lifecycle"][0]
        self.assertFalse(lifecycle["selected"])
        self.assertEqual(lifecycle["action"], "research_only_projection")
        self.assertEqual(lifecycle["exclude_reason"], "approved_execution_research_policy_missing")
        self.assertEqual(len(snapshot["content_hash"]), 64)

    def test_snapshot_never_invents_missing_arm_decisions(self):
        snapshot = collect_three_arm_snapshot({}, {"items": [{"symbol": "000001.SZ", "states": {"entry_state": "waiting"}}]},
                                              captured_at="2026-09-20T12:00:00+00:00")
        self.assertEqual(snapshot["arms"]["current_baseline"], [])
        self.assertTrue(any(row["field"] == "existing_selection_or_exclusion" for row in snapshot["missing_fields"]))
        self.assertFalse(snapshot["arms"]["lifecycle"][0]["selected"])

    def test_snapshot_supports_persisted_lane_list_shape(self):
        scan = {"lanes": [{"key": "accumulation", "selected": [
            {"symbol": "000001.SZ", "rank": 3, "eligibility": "eligible", "action": "recommend"}
        ]}]}
        snapshot = collect_three_arm_snapshot(scan, {"items": []},
                                              captured_at="2026-09-20T12:00:00+00:00")
        baseline = snapshot["arms"]["current_baseline"][0]
        self.assertEqual((baseline["lane"], baseline["eligibility"], baseline["action"]),
                         ("accumulation", "eligible", "recommend"))
        self.assertEqual(snapshot["arms"]["pure_machine"][0]["rankings"][0]["rank"], 3)

    def test_same_cost_execution_and_paired_holdout(self):
        contract = {"bars": bars(), "sessions": ["2026-09-16", "2026-09-17"]}
        decisions = {arm: {"selected": True, "fill_contract": contract, "execution_policy": self.POLICY}
                     for arm in ("current_baseline", "pure_machine", "lifecycle")}
        row = {"record_id": "a", "cohort_id": "lane:accumulation", "symbol": "000001.SZ", "event_id": "event-1", "signal_date": "2026-09-15",
               "available_at": "2026-09-15T07:00:00+00:00", "decision_cutoff_at": "2026-09-15T08:00:00+00:00",
               "decisions": decisions, "discovery_returns": {"current_baseline": 8}}
        result = evaluate_three_arms([row], train_end="2026-09-14", holdout_end="2026-09-20", minimum_holdout=2)
        self.assertEqual(result["paired"]["count"], 1)
        self.assertEqual(result["arms"]["lifecycle"]["holdout_status"], "insufficient")
        self.assertIsNotNone(result["arms"]["lifecycle"]["hypothetical_net_return_pct"])
        self.assertEqual(result["arms"]["lifecycle"]["discovery_return_pct"], None)

    def test_missing_decisions_and_unknown_returns_are_excluded_not_zero(self):
        row = {"record_id": "a", "cohort_id": "lane:accumulation", "symbol": "000001.SZ", "event_id": "same", "signal_date": "2026-09-15",
               "available_at": "2026-09-15T07:00:00+00:00", "decisions": {
                   "current_baseline": {"selected": False, "exclude_reason": "not_selected"},
                   "pure_machine": {"selected": True},
               }}
        later = {**row, "record_id": "b", "available_at": "2026-09-15T08:00:00+00:00"}
        result = evaluate_three_arms([later, row], train_end="2026-09-14", holdout_end="2026-09-20")
        self.assertEqual(result["duplicate_events_excluded"], 1)
        self.assertEqual(result["arms"]["pure_machine"]["exclusions"]["approved_execution_research_policy_missing"], 1)
        self.assertEqual(result["arms"]["lifecycle"]["exclusions"]["decision_record_missing"], 1)
        self.assertIsNone(result["arms"]["pure_machine"]["hypothetical_net_return_pct"])

    def test_approved_policy_without_fill_contract_is_distinct(self):
        row = {"cohort_id": "lane:accumulation", "symbol": "000001.SZ", "event_id": "no-fill",
               "signal_date": "2026-09-15", "available_at": "2026-09-15T07:00:00+00:00",
               "decisions": {"current_baseline": {"selected": True, "execution_policy": self.POLICY}}}
        result = evaluate_three_arms([row], train_end="2026-09-14", holdout_end="2026-09-20")
        self.assertEqual(result["arms"]["current_baseline"]["exclusions"]["executable_fill_contract_missing"], 1)
        self.assertEqual(result["arms"]["lifecycle"]["exclusions"]["decision_record_missing"], 1)

    def test_late_available_is_not_admitted(self):
        row = {"cohort_id": "lane:accumulation", "symbol": "000001.SZ", "event_id": "late", "signal_date": "2026-09-15",
               "available_at": "2026-09-16T08:00:00+00:00", "decision_cutoff_at": "2026-09-15T08:00:00+00:00",
               "decisions": {}}
        result = evaluate_three_arms([row], train_end="2026-09-14", holdout_end="2026-09-20")
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["arms"]["lifecycle"]["exclusions"]["late_available_record"], 1)

    def test_discovery_close_cannot_be_a_fill_and_cohort_cannot_mix(self):
        contract = {"bars": bars(), "sessions": ["2026-09-15", "2026-09-17"]}
        row = {"cohort_id": "one", "symbol": "000001.SZ", "event_id": "event", "signal_date": "2026-09-15",
               "available_at": "2026-09-15T07:00:00+00:00", "decisions": {
                   arm: {"selected": True, "fill_contract": contract, "execution_policy": self.POLICY}
                   for arm in ("current_baseline", "pure_machine", "lifecycle")}}
        result = evaluate_three_arms([row], train_end="2026-09-14", holdout_end="2026-09-20")
        self.assertEqual(result["arms"]["lifecycle"]["exclusions"]["discovery_close_as_entry_forbidden"], 1)
        with self.assertRaisesRegex(ValueError, "one explicit frozen cohort"):
            evaluate_three_arms([row, {**row, "cohort_id": "two", "event_id": "other"}],
                                train_end="2026-09-14", holdout_end="2026-09-20")


if __name__ == "__main__":
    unittest.main()

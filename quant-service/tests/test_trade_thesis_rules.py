import copy
import unittest

from app.strategy_origin import select_primary_origin
from app.trade_thesis import AntiRenewalError, PointInTimeError, evaluate_thesis
from app.trade_thesis.presentation import evaluation_summary
from app.trade_thesis.evidence import amount_comparisons


def condition(identifier, purpose, metric, operator, threshold, *, basis="settled_daily", benchmark="original_structure", unit="CNY"):
    return {"condition_id": identifier, "purpose": purpose, "metric": metric, "operator": operator,
            "threshold": threshold, "unit": unit, "basis": basis, "window": "current",
            "benchmark": benchmark, "confirmation": "single", "evidence_requirement": "exact_basis_unit_benchmark",
            "derivation": "identity", "severity": "normal", "version": "v1"}


def thesis(**updates):
    value = {"thesis_id": "huatian-platform", "revision": 1, "symbol": "002185.SZ", "source_run_id": "scan-0917",
             "claim": "原平台结构仍在，等待有证据的确认", "available_at": "2026-09-17T16:00:00+08:00",
             "effective_from": "2026-09-17T16:00:00+08:00", "terminal_deadline": "2026-09-25T15:00:00+08:00",
             "origin_mode": "prospective", "invariants": [condition("platform", "structure_support", "close", "gte", 11.0)],
             "confirmation_scenarios": [condition("confirm", "confirmation", "close", "gte", 12.0)],
             "invalidation_conditions": [condition("break", "invalidation", "close", "lt", 11.0)],
             "entry_conditions": [condition("entry", "entry_confirmation", "close", "gte", 12.0)],
             "cancel_conditions": [condition("cancel", "entry_cancel", "close", "lt", 11.0)]}
    value.update(updates)
    return value


def evidence(value=11.5, **updates):
    row = {"evidence_id": "bar-close-v1", "metric": "close", "value": value, "unit": "CNY",
           "basis": "settled_daily", "benchmark": "original_structure", "effective_at": "2026-09-18T15:00:00+08:00",
           "available_at": "2026-09-18T15:10:00+08:00", "availability_basis": "provider_response", "version": "v1"}
    row.update(updates)
    return row


class TradeThesisRulesTests(unittest.TestCase):
    cutoff = "2026-09-18T16:00:00+08:00"

    def test_huatian_positive_and_negative_are_symmetric_without_fitted_grace(self):
        positive = evaluate_thesis(thesis(), [evidence(12.1)], self.cutoff)
        negative = evaluate_thesis(thesis(), [evidence(10.9)], self.cutoff)
        self.assertEqual(positive["states"], {"thesis_state": "supported", "evidence_status": "complete", "entry_state": "eligible"})
        self.assertEqual(negative["states"], {"thesis_state": "invalidated", "evidence_status": "complete", "entry_state": "cancelled"})

    def test_amount_baselines_are_reported_separately_without_a_fitted_label(self):
        ratios = amount_comparisons(35.02, 43.22, 33.13)
        self.assertAlmostEqual(ratios["versus_previous"], 0.81, places=2)
        self.assertAlmostEqual(ratios["versus_mean5"], 1.06, places=2)
        self.assertNotIn("significant_selloff", ratios)

    def test_wording_or_source_rerun_does_not_change_deterministic_states(self):
        a = evaluate_thesis(thesis(), [evidence()], self.cutoff)
        b = evaluate_thesis(thesis(claim="用户改了问法", source_run_id="model-rerun"), [evidence()], self.cutoff)
        self.assertEqual(a["states"], b["states"])

    def test_intraday_breach_does_not_satisfy_settled_daily_condition(self):
        result = evaluate_thesis(thesis(), [evidence(10.8, basis="forming_intraday")], self.cutoff)
        # Metric identity alone is insufficient: this fixture deliberately uses a different metric.
        result = evaluate_thesis(thesis(), [evidence(10.8, metric="minute_low", basis="minute")], self.cutoff)
        self.assertEqual(result["states"]["thesis_state"], "pending")
        self.assertEqual(result["states"]["entry_state"], "unknown")

    def test_late_and_derived_late_evidence_are_excluded_before_evaluation(self):
        late = evidence(12.5, available_at="2026-09-18T16:01:00+08:00")
        derived = evidence(12.5, evidence_id="derived-rs", derivation="ratio", dependencies=[
            {"evidence_id": "sector-close", "available_at": "2026-09-18T16:02:00+08:00", "content_hash": "a"}])
        result = evaluate_thesis(thesis(), [late, derived], self.cutoff)
        self.assertFalse(result["evidence_manifest"]["pit_eligible"])
        self.assertEqual(result["states"]["entry_state"], "unknown")
        self.assertEqual({x["reason"] for x in result["evidence_manifest"]["excluded"]},
                         {"available_after_cutoff", "dependency_available_after_cutoff"})

    def test_cutoff_rejects_future_thesis_and_is_deterministic(self):
        with self.assertRaises(PointInTimeError):
            evaluate_thesis(thesis(available_at="2026-09-19T00:00:00+08:00"), [], self.cutoff)
        a = evaluate_thesis(thesis(), [evidence()], self.cutoff)
        b = evaluate_thesis(thesis(), [evidence()], self.cutoff)
        self.assertEqual((a["evaluation_id"], a["content_hash"]), (b["evaluation_id"], b["content_hash"]))
        changed = evaluate_thesis(thesis(), [evidence(11.6)], self.cutoff)
        self.assertNotEqual(a["evaluation_id"], changed["evaluation_id"])

    def test_new_run_name_or_lane_cannot_extend_deadline(self):
        prior = evaluate_thesis(thesis(), [evidence()], self.cutoff)
        with self.assertRaises(AntiRenewalError):
            evaluate_thesis(thesis(source_run_id="rerun", terminal_deadline="2026-09-26T15:00:00+08:00"), [evidence()], self.cutoff, prior)

    def test_approved_new_episode_can_extend_but_does_not_rewrite_previous(self):
        old = thesis(); prior = evaluate_thesis(old, [evidence()], self.cutoff)
        new = thesis(thesis_id="huatian-new-event", terminal_deadline="2026-09-26T15:00:00+08:00",
                     new_episode_review={"decision": "approved", "incremental_evidence_refs": ["event-1"],
                                         "deadline_reason": "new event", "author": "agent-a", "reviewer": "agent-b"})
        evaluate_thesis(new, [evidence()], self.cutoff, {**prior, "terminal_deadline": old["terminal_deadline"]})
        self.assertEqual(old["terminal_deadline"], "2026-09-25T15:00:00+08:00")

    def test_correction_is_append_only_conflict_until_old_version_superseded(self):
        corrected = evidence(12.2, evidence_id="bar-close-v2", version="v2")
        conflict = evaluate_thesis(thesis(), [evidence(11.5), corrected], self.cutoff)
        self.assertEqual(conflict["states"]["evidence_status"], "conflict")
        old = evidence(11.5, superseded_at="2026-09-18T15:30:00+08:00")
        fixed = evaluate_thesis(thesis(), [old, corrected], self.cutoff)
        self.assertEqual(fixed["observations"][0]["evidence_id"], "bar-close-v2")

    def test_benchmark_change_is_not_direct_numeric_change(self):
        relative = thesis(invariants=[condition("relative", "structure_support", "relative_strength", "gte", 0, unit="ratio", benchmark="ETF")],
                          confirmation_scenarios=[], invalidation_conditions=[])
        prior = evaluate_thesis(relative, [evidence(0.1, metric="relative_strength", unit="ratio", benchmark="ETF")], self.cutoff)
        relative["invariants"][0]["benchmark"] = "industry_median"
        current = evaluate_thesis(relative, [evidence(0.1, evidence_id="industry", metric="relative_strength", unit="ratio", benchmark="industry_median")], self.cutoff, prior)
        change = current["changes_since_previous"][0]
        self.assertEqual(change["impact"], "comparison_basis_changed")
        self.assertFalse(change["directly_comparable"])

    def test_terminal_superseded_cannot_become_entry_eligible(self):
        result = evaluate_thesis(thesis(superseded_by="revision-2"), [evidence(12.5)], self.cutoff)
        self.assertEqual(result["states"]["thesis_state"], "superseded")
        self.assertEqual(result["states"]["entry_state"], "cancelled")

    def test_new_episode_never_mutates_or_extends_holding_clock(self):
        prior = evaluate_thesis(thesis(), [evidence()], self.cutoff)
        prior["holding_timeline"] = {"started_at": "2026-09-10", "day3_due": "2026-09-15"}
        before = copy.deepcopy(prior["holding_timeline"])
        new = thesis(thesis_id="independent-event", terminal_deadline="2026-09-26T15:00:00+08:00",
                     new_episode_review={"decision": "approved", "incremental_evidence_refs": ["event-2"],
                                         "deadline_reason": "independent event", "author": "agent-a", "reviewer": "agent-b"})
        result = evaluate_thesis(new, [evidence()], self.cutoff, prior)
        self.assertNotIn("holding_timeline", result)
        self.assertEqual(prior["holding_timeline"], before)

    def test_future_effective_evidence_and_thesis_fail_closed(self):
        result = evaluate_thesis(thesis(), [evidence(effective_at="2026-09-19T15:00:00+08:00")], self.cutoff)
        self.assertEqual(result["states"]["entry_state"], "unknown")
        self.assertEqual(result["evidence_manifest"]["excluded"][0]["reason"], "effective_after_cutoff")
        with self.assertRaises(PointInTimeError):
            evaluate_thesis(thesis(effective_from="2026-09-19T09:30:00+08:00"), [], self.cutoff)

    def test_non_finite_market_value_is_never_eligible_or_hashed_as_json_nan(self):
        result = evaluate_thesis(thesis(), [evidence(float("nan"))], self.cutoff)
        self.assertEqual(result["states"]["entry_state"], "unknown")
        self.assertEqual(result["evidence_manifest"]["excluded"][0]["reason"], "non_finite_value")

    def test_unimplemented_sustained_window_is_unknown_not_single_value_true(self):
        sustained = condition("three-minutes", "entry_confirmation", "minute_above_reference", "eq", True,
                              basis="forming_intraday", benchmark="original_reference", unit="bool")
        sustained.update(window="3_minutes", confirmation="3_consecutive")
        contract = thesis(entry_conditions=[sustained])
        row = evidence(True, metric="minute_above_reference", basis="forming_intraday",
                       benchmark="original_reference", unit="bool")
        result = evaluate_thesis(contract, [evidence(12.1), row], self.cutoff)
        entry_result = next(item for item in result["condition_results"] if item["condition_id"] == "three-minutes")
        self.assertEqual((entry_result["result"], entry_result["reason"]), ("unknown", "unsupported_contract"))
        self.assertEqual(result["states"]["entry_state"], "unknown")
        self.assertEqual(result["states"]["evidence_status"], "partial")

    def test_supported_structure_remains_supported_when_unstructured_entry_is_partial(self):
        unsupported_entry = condition("aggregate-entry", "entry_confirmation", "entry_score", "gte", 1,
                                      basis="scenario", benchmark="original_entry_scenario", unit="score")
        unsupported_entry["derivation"] = "five_bar_aggregate"
        contract = thesis(entry_conditions=[unsupported_entry])
        rows = [evidence(12.1), evidence(2, metric="entry_score", basis="scenario",
                                       benchmark="original_entry_scenario", unit="score")]
        result = evaluate_thesis(contract, rows, self.cutoff)
        self.assertEqual(result["states"]["thesis_state"], "supported")
        self.assertEqual(result["states"]["entry_state"], "unknown")
        self.assertEqual(result["states"]["evidence_status"], "partial")

    def test_same_metric_wrong_basis_or_benchmark_does_not_make_evidence_complete(self):
        wrong = evidence(12.1, basis="forming_intraday", benchmark="intraday_quote")
        result = evaluate_thesis(thesis(), [wrong], self.cutoff)
        close_results = [item for item in result["condition_results"] if item.get("metric") == "close"]
        self.assertTrue(close_results)
        self.assertTrue(all(item["result"] == "unknown" for item in close_results))
        self.assertEqual(result["states"]["evidence_status"], "partial")
        self.assertNotEqual(result["states"]["entry_state"], "eligible")

    def test_no_machine_evaluable_conditions_is_partial_not_vacuously_complete(self):
        contract = thesis(invariants=[], confirmation_scenarios=[], invalidation_conditions=[],
                          entry_conditions=[], cancel_conditions=[])
        result = evaluate_thesis(contract, [], self.cutoff)
        self.assertEqual(result["states"]["evidence_status"], "partial")
        self.assertEqual(result["states"]["entry_state"], "waiting")

    def test_same_session_cannot_self_approve_deadline_extension(self):
        prior = evaluate_thesis(thesis(), [evidence()], self.cutoff)
        proposed = thesis(terminal_deadline="2026-09-26T15:00:00+08:00",
                          new_episode_review={"decision": "approved", "incremental_evidence_refs": ["event"],
                                              "deadline_reason": "event", "author": "same", "reviewer": "same"})
        with self.assertRaises(AntiRenewalError):
            evaluate_thesis(proposed, [evidence()], self.cutoff, prior)

    def test_expiry_is_not_invalidation_and_cannot_be_revived(self):
        expired = evaluate_thesis(thesis(terminal_deadline=self.cutoff), [evidence(12.5)], self.cutoff)
        self.assertEqual(expired["states"]["thesis_state"], "expired")
        later = evaluate_thesis(thesis(terminal_deadline=self.cutoff), [evidence(12.5)], "2026-09-19T16:00:00+08:00", expired)
        self.assertEqual(later["states"]["thesis_state"], "expired")

    def test_input_contracts_are_not_mutated_and_summary_is_factual(self):
        t, rows = thesis(), [evidence()]; before = copy.deepcopy((t, rows))
        result = evaluate_thesis(t, rows, self.cutoff)
        self.assertEqual((t, rows), before)
        text = evaluation_summary(result)
        self.assertIn("新买 waiting", text)
        self.assertNotIn("买入", text)


class StrategyOriginTests(unittest.TestCase):
    def test_order_does_not_change_primary_and_stage_is_only_initial_preference(self):
        memberships = [{"lane": "trend", "rank": 2, "origin_id": "o2"},
                       {"lane": "expansion", "rank": 40, "origin_id": "o1"}]
        item = {"stage": "initial_breakout", "memberships": memberships}
        a = select_primary_origin(item)
        b = select_primary_origin({**item, "memberships": list(reversed(memberships))})
        self.assertEqual(a, b)
        self.assertEqual(a["origin_id"], "o1")

    def test_explicit_binding_wins_and_conflict_fails_closed(self):
        item = {"primary_origin_id": "o2", "memberships": [{"lane": "a", "rank": 1, "origin_id": "o1"},
                                                              {"lane": "b", "rank": 9, "origin_id": "o2"}]}
        self.assertEqual(select_primary_origin(item)["lane"], "b")
        with self.assertRaisesRegex(ValueError, "binding_conflict"):
            select_primary_origin({**item, "primary_origin_id": "missing"})
        with self.assertRaisesRegex(ValueError, "lane_missing"):
            select_primary_origin({"memberships": [{"rank": 1, "origin_id": "o1"}]})


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy

from app.trade_thesis.scenarios import Scenario, evaluate_scenario_intersection


def condition(identifier, purpose="entry"):
    metric = "cancel_scenario_confirmed" if identifier == "cancel" else "full_entry_scenario_confirmed"
    return {"condition_id": identifier, "purpose": purpose, "metric": metric,
            "operator": "eq", "threshold": True, "unit": "bool", "basis": "scenario", "window": "current",
            "benchmark": "original_entry_scenario", "confirmation": "single",
            "evidence_requirement": "exact_basis_unit_benchmark", "derivation": "identity",
            "severity": "required", "version": "v1"}


def scenario(**updates):
    value = {"scenario_id": "s1", "thesis_revision": 1, "entry_conditions": [condition("entry")],
             "cancel_conditions": [condition("cancel", "entry_cancel")], "max_entry_price": "12.00",
             "valid_until": "2026-09-22T15:00:00+08:00", "execution_basis": "forming_intraday"}
    value.update(updates)
    return value


def thesis(entry="eligible", thesis_state="supported", cancel=False):
    return {"thesis_id": "t1", "thesis_revision": 1,
            "symbol": "002185.SZ",
            "states": {"thesis_state": thesis_state, "evidence_status": "complete", "entry_state": entry},
            "condition_results": [{"condition_id": "entry", "purpose": "entry", "result": "true"},
                                  {"condition_id": "cancel", "purpose": "entry_cancel",
                                   "result": "true" if cancel else "false"}],
            "observations": [
                {"evidence_id": "entry-e1", "metric": "full_entry_scenario_confirmed", "value": True,
                 "unit": "bool", "basis": "scenario", "benchmark": "original_entry_scenario"},
                {"evidence_id": "cancel-e1", "metric": "cancel_scenario_confirmed", "value": cancel,
                 "unit": "bool", "basis": "scenario", "benchmark": "original_entry_scenario"},
            ]}


def plan(kind="new_buy", price="11.80", quantity=None):
    return {"plan_id": "p1", "account_key": "a", "symbol": "002185.SZ", "plan_kind": kind, "status": "active",
            "valid_until": "2026-09-23T15:00:00+08:00", "sizing": {"sizing_price": "9.99"},
            "lines": [{"kind": "chase_cap", "price": price}],
            "position": None if quantity is None else {"quantity": quantity}}


def execution(price="11.70"):
    return {"quote_available_at": "2026-09-21T10:00:00+08:00", "current_price": price,
            "quote_valid_until": "2026-09-21T10:01:00+08:00",
            "is_suspended": False, "tradability_status": "verified"}


def binding():
    return {"account_key": "a", "symbol": "002185.SZ", "position_episode_id": "e1",
            "thesis_id": "t1", "revision": 1, "plan_id": "p1", "binding_source": "user_confirmed",
            "bound_at": "2026-09-20T10:00:00+08:00", "evidence_refs": ["trade-1"]}


def evaluate(*, thesis_value=None, plan_value=None, discipline=None, binding_value=None, execution_value=None, scenario_value=None):
    return evaluate_scenario_intersection(
        scenario_value or scenario(), thesis_value or thesis(), discipline_plan=plan_value,
        discipline_evaluation=discipline, binding=binding_value, execution_evidence=execution_value,
        as_of="2026-09-21T10:00:00+08:00")


def test_t06_new_buy_cancel_does_not_exit_an_active_holding_plan():
    result = evaluate(thesis_value=thesis(entry="cancelled", cancel=True), plan_value=plan("holding", quantity=1000),
                      discipline={"plan_state": "active"}, binding_value=binding(), execution_value=execution())
    assert result["combined_entry_state"] == "blocked"
    assert result["holding"] == {"binding_status": "bound", "plan_state": "active", "hard_risk": False,
                                  "action": "hold_plan_active", "position_quantity": 1000, "action_quantity": None}


def test_t07_supported_research_and_hard_risk_are_both_preserved():
    result = evaluate(plan_value=plan("holding", quantity=1000), discipline={"plan_state": "exit_signalled"},
                      binding_value=binding(), execution_value=execution())
    assert result["research_entry_state"] == "eligible"
    assert result["holding"]["action"] == "exit_signalled"
    assert result["holding"]["hard_risk"] is True
    assert "discipline_risk_signal:exit_signalled" in result["conflict_codes"]
    assert next(item for item in result["conflicts"] if item["code"] == "discipline_risk_signal")["message"]
    assert result["combined_entry_state"] == "blocked"


def test_t13_settled_close_can_only_wait_for_next_session_and_never_claim_fill_or_return():
    settled = scenario(execution_basis="settled_daily_next_session")
    result = evaluate(scenario_value=settled, plan_value=plan(), discipline={"plan_state": "active"},
                      execution_value={**execution(), "next_session": "2026-09-22"})
    assert result["combined_entry_state"] == "waiting"
    assert result["execution"]["state"] == "next_session_recheck"
    assert result["execution"]["earliest_session"] == "2026-09-22"
    assert result["execution"]["verified_fill"] is False
    assert result["execution"]["realized_return"] is None


def test_t13_missing_quote_suspension_or_tradability_evidence_is_unknown_not_a_return():
    result = evaluate(plan_value=plan(), discipline={"plan_state": "active"}, execution_value={})
    assert result["combined_entry_state"] == "unknown"
    assert result["execution"]["state"] == "unknown"
    assert result["execution"]["verified_fill"] is False


def test_t14_research_eligible_but_discipline_price_range_conflict_blocks_entry_with_reason():
    result = evaluate(plan_value=plan(price="11.50"), discipline={"plan_state": "active"},
                      execution_value=execution(price="11.70"))
    assert result["research_entry_state"] == "eligible"
    assert result["combined_entry_state"] == "blocked"
    assert "price_above_allowed_cap:11.70>11.50" in result["conflict_codes"]
    assert next(item for item in result["conflicts"] if item["code"] == "price_above_allowed_cap")["detail"] == "11.70>11.50"
    assert result["price_limits"]["effective_max_entry_price"] == "11.50"


def test_unbound_projection_never_infers_shares_or_holding_action():
    result = evaluate(plan_value=plan("holding", quantity=2000), discipline={"plan_state": "exit_signalled"},
                      execution_value=execution())
    assert result["holding"]["binding_status"] == "unbound"
    assert result["holding"]["position_quantity"] is None
    assert result["holding"]["action_quantity"] is None
    assert result["holding"]["action"] == "no_inferred_action"


def test_missing_account_plan_keeps_research_eligible_but_intersection_unknown():
    result = evaluate(execution_value=execution())
    assert result["research_entry_state"] == "eligible"
    assert result["scenario_entry_state"] == "eligible"
    assert result["combined_entry_state"] == "unknown"
    assert result["missing_context"] == ["discipline_plan_missing", "discipline_evaluation_missing"]


def test_scenario_condition_unknown_cannot_borrow_overall_research_eligibility():
    value = thesis()
    value["observations"] = []
    result = evaluate(thesis_value=value, plan_value=plan(), discipline={"plan_state": "active"},
                      execution_value=execution())
    assert result["research_entry_state"] == "eligible"
    assert result["scenario_entry_state"] == "unknown"
    assert result["combined_entry_state"] == "unknown"


def test_inputs_are_advisory_and_not_mutated():
    thesis_value, plan_value, binding_value = thesis(), plan(), binding()
    before = deepcopy((thesis_value, plan_value, binding_value))
    result = evaluate(thesis_value=thesis_value, plan_value=plan_value, discipline={"plan_state": "active"},
                      binding_value=binding_value, execution_value=execution())
    assert (thesis_value, plan_value, binding_value) == before
    assert result["advisory_only"] and result["live_effect"] == "none" and not result["decision_binding"]


def test_same_condition_id_with_different_threshold_is_re_evaluated_from_observations():
    changed = scenario(entry_conditions=[{**condition("entry"), "threshold": False}])
    result = evaluate(scenario_value=changed, plan_value=plan(), discipline={"plan_state": "active"},
                      execution_value=execution())
    assert result["research_entry_state"] == "eligible"
    assert result["scenario_entry_state"] == "waiting"
    assert result["combined_entry_state"] == "unknown"


def test_future_stale_or_malformed_quote_is_unknown():
    cases = [
        {**execution(), "quote_available_at": "2026-09-21T10:00:01+08:00"},
        {**execution(), "quote_valid_until": "2026-09-21T09:59:59+08:00"},
        {**execution(), "current_price": float("nan")},
        {**execution(), "current_price": 0},
        {**execution(), "is_suspended": None},
        {**execution(), "tradability_status": "unknown"},
    ]
    for evidence in cases:
        result = evaluate(plan_value=plan(), discipline={"plan_state": "active"}, execution_value=evidence)
        assert result["combined_entry_state"] == "unknown"
        assert result["execution"]["state"] == "unknown"
        assert result["execution"]["reason"].startswith("执行证据不足：")


def test_sizing_price_is_not_misused_as_an_authorized_entry_cap():
    value = plan(price="11.90")
    value["sizing"]["sizing_price"] = "10.00"
    value["lines"] = []  # no explicit chase_cap or entry constraint
    result = evaluate(plan_value=value, discipline={"plan_state": "active"}, execution_value=execution("11.70"))
    assert result["combined_entry_state"] == "eligible"
    assert result["price_limits"]["discipline_max_entry_price"] is None
    assert result["price_limits"]["effective_max_entry_price"] == "12.00"


def test_binding_must_match_account_symbol_plan_and_precede_cutoff():
    mismatches = [
        {**binding(), "symbol": "600000.SH"},
        {**binding(), "account_key": "other"},
        {**binding(), "plan_id": "other-plan"},
        {**binding(), "bound_at": "2026-09-21T10:00:01+08:00"},
    ]
    for value in mismatches:
        result = evaluate(plan_value=plan(), discipline={"plan_state": "active"},
                          binding_value=value, execution_value=execution())
        assert result["holding"]["binding_status"] == "mismatch"
        assert result["holding"]["position_quantity"] is None
        assert result["combined_entry_state"] == "blocked"


def test_expired_plan_never_exposes_position_as_actionable_quantity():
    stale = plan("holding", quantity=2500)
    stale["valid_until"] = "2026-09-20T15:00:00+08:00"
    result = evaluate(plan_value=stale, discipline={"plan_state": "expired"},
                      binding_value=binding(), execution_value=execution())
    assert result["holding"]["position_quantity"] is None
    assert result["holding"]["action_quantity"] is None
    assert result["combined_entry_state"] == "blocked"

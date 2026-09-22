import copy
import unittest

from app.intraday_actions.engine import evaluate
from app.intraday_actions.episodes import reduce_episode


def fixture(action="first_buy"):
    buy = dict(basis="completed_1m", version="m1", kind="breakout", confirmation_minutes=3,
               reference=10, quantity=300, max_buy_price=10.5, volume_baseline=100,
               min_volume_ratio=1.2, sector_min_change=0)
    rule = copy.deepcopy(buy)
    if action == "reduce":
        rule.update(kind="breakdown", reference=10.5)
    if action == "exit":
        rule.update(kind="hard_stop", reference=10.5)
    if action == "t_buy_first":
        rule.update(kind="pullback_reclaim", target_price=10.8, min_net_profit=20,
                    second_leg={**buy, "reference": 10.8, "kind": "breakout"})
    if action == "t_sell_first":
        rule.update(kind="exhaustion", reference=10.5, peak_reference=11, min_pullback_pct=3,
                    target_price=9.8, min_sell_price=10.1, min_net_profit=20,
                    second_leg={**buy, "reference": 9.7, "max_buy_price": 9.9})
    role = "recommendation" if action == "first_buy" else "holding"
    rows = [dict(end_at=f"2026-09-23T10:0{i}:00+08:00", close=10.2, high=10.3,
                 low=10.1, vwap=10, volume=150, completed=True, source="licensed") for i in range(1, 5)]
    if action == "t_buy_first":
        rows[0]["low"] = 9.9
    if action == "t_sell_first":
        for r, price in zip(rows, (10.5, 10.4, 10.3, 10.2)):
            r.update(close=price, low=price-.1, high=11, vwap=10.6)
    return dict(now="2026-09-23T10:04:05+08:00", account_key="citics-primary", symbol="600000.SH", name="测试股票",
                role=role, formal_recommendation=True,
                quote=dict(price=10.2, as_of="2026-09-23T10:04:04+08:00", source="licensed", ask_available=True, bid_available=True),
                minutes=rows,
                policy=dict(max_quote_age_seconds=15, max_minute_age_seconds=150, max_account_age_seconds=3600,
                            max_market_age_seconds=60, max_price_deviation_pct=1),
                market=dict(session_open=True, sector_confirmed=True, sector_key="sector", source="licensed",
                            sector_change_pct=1, as_of="2026-09-23T10:04:00+08:00"),
                plan=dict(id="plan", version="v1", created_at="2026-09-23T10:00:00+08:00", expires_at="2026-09-23T15:00:00+08:00",
                          quality_status="accepted", hard_stop=10.5 if action == "exit" else 9,
                          allow_buy=True, allow_add=True, thesis_valid=True, risk_budget_pct=.05,
                          source_rank=1, decision_id="decision", snapshot_id="snapshot", rules={action: rule}),
                account=dict(snapshot_id="snapshot", as_of="2026-09-23T10:00:00+08:00", quantity_verified=True,
                             total_assets=100000, stock_market_value=10000, allocated_buy_cash=0, lot_size=100,
                             held_quantity=0 if role == "recommendation" else 1000,
                             sellable_quantity=0 if role == "recommendation" else 500, position_cost=10, position_risk=1000,
                             costs=dict(source="account-contract", as_of="2026-09-23T09:00:00+08:00", commission_rate=.0003,
                                        min_commission=5, stamp_tax_sell_rate=.0005, transfer_rate=.00001, slippage_bps=5)))


def arm(context):
    result = evaluate(context)
    context["previous_states"] = result["states"]
    for row in context["previous_states"].values():
        row.update(state="waiting", consumed=False)
    return context


def episode(action="t_buy_first"):
    return dict(id="episode", action=action, account_key="citics-primary", symbol="600000.SH", session_date="2026-09-23",
                target_quantity=300, old_sellable_quantity=500, created_at="2026-09-23T09:50:00+08:00", fills=[])


def fill(fid="fill-1", leg="first", qty=100, action="t_buy_first"):
    side = "buy" if (action == "t_buy_first") == (leg == "first") else "sell"
    return dict(fill_id=fid, account_key="citics-primary", symbol="600000.SH", leg=leg, side=side,
                quantity=qty, price=10 if side == "buy" else 11,
                occurred_at="2026-09-23T10:00:00+08:00", observed_at="2026-09-23T10:00:01+08:00",
                source="broker_import", verified=True)


class IntradayActionTests(unittest.TestCase):
    def test_six_positive_actions(self):
        for action in ("first_buy", "add", "reduce", "exit", "t_buy_first", "t_sell_first"):
            with self.subTest(action=action):
                result = evaluate(arm(fixture(action)))
                self.assertEqual([e["action"] for e in result["events"]], [action], result["blockers"])
                self.assertFalse(result["orders_authorized"])

    def test_six_negative_conditions(self):
        for action in ("first_buy", "add", "reduce", "exit", "t_buy_first", "t_sell_first"):
            with self.subTest(action=action):
                c = arm(fixture(action))
                for row in c["minutes"]:
                    row.update(close=10.6, low=10.5, high=10.7)
                c["quote"]["price"] = 10.6
                self.assertEqual(evaluate(c)["events"], [])

    def test_baseline_and_restart_never_repeat(self):
        c = fixture()
        first = evaluate(c)
        self.assertFalse(first["events"])
        self.assertTrue(first["states"]["first_buy"]["baseline_suppressed"])
        c["previous_states"] = first["states"]
        self.assertFalse(evaluate(c)["events"])
        c = arm(fixture())
        event = evaluate(c)
        c["previous_states"] = event["states"]
        self.assertFalse(evaluate(c)["events"])

    def test_true_wait_to_confirm_transition(self):
        c = fixture()
        for row in c["minutes"]:
            row["volume"] = 10
        wait = evaluate(c)
        self.assertEqual(wait["states"]["first_buy"]["state"], "waiting")
        c["previous_states"] = wait["states"]
        for row in c["minutes"]:
            row["volume"] = 150
        self.assertEqual(len(evaluate(c)["events"]), 1)

    def test_daily_basis_and_precreation_minutes_rejected(self):
        c = arm(fixture())
        c["plan"]["rules"]["first_buy"]["basis"] = "daily_close"
        self.assertIn("explicit_minute_basis_required", str(evaluate(c)["blockers"]))
        c = arm(fixture())
        c["plan"]["created_at"] = "2026-09-23T10:03:00+08:00"
        self.assertIn("post_plan_minutes_insufficient", str(evaluate(c)["blockers"]))

    def test_stale_quote_duplicate_minute_and_missing_sector(self):
        cases = (("stale", "quote_stale_or_future"), ("duplicate", "minute_not_strictly_ordered"), ("sector", "sector_confirmation_missing"))
        for kind, blocker in cases:
            c = arm(fixture())
            if kind == "stale":
                c["quote"]["as_of"] = "2026-09-23T09:30:00+08:00"
            elif kind == "duplicate":
                c["minutes"].append(copy.deepcopy(c["minutes"][-1]))
            else:
                c["market"]["sector_confirmed"] = False
            result = evaluate(c)
            self.assertFalse(result["events"])
            self.assertIn(blocker, str(result["blockers"]))

    def test_future_and_incomplete_minutes_not_consumed(self):
        c = arm(fixture())
        old = evaluate(c)
        c["minutes"].append({**c["minutes"][-1], "end_at": "2026-09-23T10:05:00+08:00", "close": 1})
        self.assertEqual(old, evaluate(c))
        c["minutes"][-1].update(end_at="2026-09-23T10:04:00+08:00", completed=False)
        self.assertEqual(old, evaluate(c))

    def test_add_requires_valid_thesis_and_no_add_wins(self):
        for field, value in (("allow_add", False), ("thesis_valid", False)):
            c = arm(fixture("add"))
            c["plan"][field] = value
            self.assertFalse(evaluate(c)["events"])

    def test_add_below_cost_with_new_structure_is_not_automatically_forbidden(self):
        c = arm(fixture("add"))
        c["account"]["position_cost"] = 11
        self.assertEqual([e["action"] for e in evaluate(c)["events"]], ["add"])
        for row in c["minutes"]:
            row["volume"] = 10
        result = evaluate(c)
        self.assertFalse(result["events"])
        self.assertEqual(result["states"]["add"]["state"], "waiting")

    def test_add_below_cost_respects_explicit_plan_prohibition(self):
        c = arm(fixture("add"))
        c["account"]["position_cost"] = 11
        c["plan"]["forbid_add_below_cost"] = True
        self.assertIn("explicit_add_below_cost_forbidden", str(evaluate(c)["blockers"]))
        c["account"].pop("position_cost")
        self.assertIn("explicit_add_cost_policy_unverifiable", str(evaluate(c)["blockers"]))
        c["plan"]["forbid_add_below_cost"] = False
        self.assertEqual([e["action"] for e in evaluate(c)["events"]], ["add"])

    def test_exit_independent_of_buy_inputs_and_t_plus_one(self):
        c = arm(fixture("exit"))
        for key in ("costs", "total_assets", "stock_market_value", "allocated_buy_cash", "position_risk"):
            c["account"].pop(key)
        c["market"].pop("sector_confirmed")
        for row in c["minutes"]:
            row.pop("vwap")
            row.pop("volume")
        result = evaluate(c)
        self.assertEqual(result["events"][0]["quantity"], 500)
        self.assertEqual(result["events"][0]["locked_quantity"], 500)
        c["account"]["sellable_quantity"] = 0
        self.assertIn("t_plus_one_no_sellable_shares", str(evaluate(c)["blockers"]))

    def test_cash_reservation_risk_and_fee_reduce_quantity(self):
        c = arm(fixture())
        c["account"]["allocated_buy_cash"] = 88000
        self.assertEqual(evaluate(c)["events"][0]["quantity"], 100)
        c["account"]["allocated_buy_cash"] = 89500
        self.assertFalse(evaluate(c)["events"])
        c = arm(fixture())
        c["account"]["position_risk"] = 5000
        self.assertFalse(evaluate(c)["events"])
        c = arm(fixture())
        c["account"].pop("costs")
        self.assertIn("account_costs_missing", str(evaluate(c)["blockers"]))

    def test_strength_and_concentration_alone_never_sell(self):
        c = arm(fixture("reduce"))
        c["account"]["stock_market_value"] = 99999
        c["plan"]["rules"]["reduce"]["reference"] = 9
        self.assertFalse(evaluate(c)["events"])
        c = arm(fixture("t_sell_first"))
        for i, row in enumerate(c["minutes"]):
            row.update(close=10 + i*.1, low=9.9, high=11)
        self.assertFalse(evaluate(c)["events"])

    def test_exit_suppresses_t_and_add_even_if_exit_consumed(self):
        c = fixture("add")
        c["plan"]["hard_stop"] = 10.5
        c["plan"]["rules"]["exit"] = fixture("exit")["plan"]["rules"]["exit"]
        c = arm(c)
        c["previous_states"]["exit"]["consumed"] = True
        self.assertFalse(evaluate(c)["events"])

    def test_limit_order_book_and_gap_open(self):
        c = arm(fixture())
        c["quote"]["ask_available"] = False
        self.assertIn("no_ask_execution_uncertain", str(evaluate(c)["blockers"]))
        c = arm(fixture())
        c["quote"]["price"] = 12
        for row in c["minutes"]:
            row.update(close=12, low=12, high=12)
        self.assertIn("chase_cap_exceeded", str(evaluate(c)["blockers"]))

    def test_t_costs_no_real_fill_and_no_repurchase_chase(self):
        c = arm(fixture("t_buy_first"))
        c["account"]["costs"]["min_commission"] = 500
        self.assertIn("t_net_spread_insufficient", str(evaluate(c)["blockers"]))
        c = arm(fixture("t_buy_first"))
        c["t_episode"] = episode()
        self.assertIn("first_leg_actual_fill_required", str(evaluate(c)["blockers"]))
        c = fixture("t_sell_first")
        ep = episode("t_sell_first")
        ep["fills"] = [{**fill(action="t_sell_first", qty=300), "price": 10}]
        c["t_episode"] = ep
        for row in c["minutes"]:
            row.update(vwap=10, low=10.1, high=10.3, close=10.2)
        c["plan"]["rules"]["t_sell_first"]["second_leg"]["max_buy_price"] = 10.5
        result = evaluate(arm(c))
        self.assertIn("t_net_spread_insufficient_no_chasing", str(result["blockers"]))

    def test_t_first_leg_net_spread_uses_worst_allowed_prices_both_directions(self):
        for action in ("t_buy_first", "t_sell_first"):
            with self.subTest(action=action):
                c = arm(fixture(action))
                self.assertEqual(len(evaluate(c)["events"]), 1)
                rule = c["plan"]["rules"][action]
                if action == "t_buy_first":
                    rule["max_buy_price"] = 10.75
                else:
                    # Target 9.8 is profitable; allowed cap 10.05 is not at
                    # the minimum permitted first sale 10.1 after fees.
                    rule["second_leg"]["max_buy_price"] = 10.05
                result = evaluate(c)
                self.assertFalse(result["events"])
                self.assertIn("t_net_spread_insufficient_no_chasing", str(result["blockers"]))

    def test_t_second_leg_net_spread_uses_verified_first_and_worst_remaining_price(self):
        for action in ("t_buy_first", "t_sell_first"):
            with self.subTest(action=action):
                c = fixture(action)
                c["t_episode"] = episode(action)
                c["t_episode"]["fills"] = [fill(action=action, qty=300)]
                price = 10.9 if action == "t_buy_first" else 9.8
                c["quote"]["price"] = price
                for row in c["minutes"]:
                    row.update(close=price, low=price-.1, high=price+.1, vwap=price-.1)
                c = arm(c)
                self.assertEqual(len(evaluate(c)["events"]), 1)
                if action == "t_buy_first":
                    # Current sale 10.9 appears profitable but permitted
                    # sale floor 10.8 against actual cost 10.75 is not.
                    c["t_episode"]["fills"][0]["price"] = 10.75
                else:
                    # Current buy 9.8 appears profitable against actual
                    # sale 10; allowed repurchase cap 10.05 loses money.
                    c["t_episode"]["fills"][0]["price"] = 10
                    c["plan"]["rules"][action]["second_leg"]["max_buy_price"] = 10.05
                result = evaluate(c)
                self.assertFalse(result["events"])
                self.assertIn("t_net_spread_insufficient_no_chasing", str(result["blockers"]))

    def test_t_sell_first_requires_declared_floor_not_current_quote(self):
        c = arm(fixture("t_sell_first"))
        c["plan"]["rules"]["t_sell_first"].pop("min_sell_price")
        self.assertIn("t_first_sell_floor_missing", str(evaluate(c)["blockers"]))
        c["plan"]["rules"]["t_sell_first"]["min_sell_price"] = 10.3
        self.assertIn("t_first_sell_floor_breached", str(evaluate(c)["blockers"]))

    def test_t_sell_exhaustion_invalid_when_latest_quote_reclaims_reference(self):
        c = fixture("t_sell_first")
        c["plan"]["rules"]["t_sell_first"].update(reference=10.25, min_sell_price=10.2)
        for row, price in zip(c["minutes"][-3:], (10.24, 10.22, 10.20)):
            row.update(close=price, low=price-.1)
        c = arm(c)
        self.assertEqual(len(evaluate(c)["events"]), 1)
        c["quote"]["price"] = 10.26
        result = evaluate(c)
        self.assertFalse(result["events"])
        self.assertEqual(result["states"]["t_sell_first"]["state"], "waiting")

    def test_t_second_leg_uses_only_actual_partial_fill_and_old_shares(self):
        c = fixture("t_buy_first")
        ep = episode()
        ep["fills"] = [fill(qty=100)]
        c["t_episode"] = ep
        c["quote"]["price"] = 10.9
        for row in c["minutes"]:
            row.update(close=10.9, high=11, low=10.8, vwap=10.5)
        result = evaluate(arm(c))
        self.assertEqual(result["events"][0]["leg"], "second")
        self.assertEqual(result["events"][0]["quantity"], 100)

    def test_pullback_requires_prior_touch_not_only_breakout(self):
        c = arm(fixture())
        c["plan"]["rules"]["first_buy"]["kind"] = "pullback_reclaim"
        self.assertFalse(evaluate(c)["events"])
        c["minutes"][0]["low"] = 9.9
        self.assertEqual(len(evaluate(c)["events"]), 1)

    def test_lunch_gap_cannot_complete_three_bar_confirmation(self):
        c = arm(fixture())
        c["now"] = "2026-09-23T13:01:05+08:00"
        c["quote"]["as_of"] = c["now"]
        c["market"]["as_of"] = c["now"]
        c["account"]["as_of"] = c["now"]
        c["minutes"] = c["minutes"][:3]
        for row, clock in zip(c["minutes"], ("11:29", "11:30", "13:01")):
            row["end_at"] = "2026-09-23T" + clock + ":00+08:00"
        self.assertIn("confirmation_crosses_session_gap", str(evaluate(c)["blockers"]))

    def test_latest_quote_must_still_hold_reference_and_vwap(self):
        c = arm(fixture())
        c["quote"]["price"] = 9.99
        c["policy"]["max_price_deviation_pct"] = 3
        self.assertFalse(evaluate(c)["events"])

    def test_late_rule_enrichment_cannot_use_pre_rule_minutes(self):
        c = arm(fixture())
        c["plan"]["rules"]["first_buy"]["created_at"] = "2026-09-23T10:03:00+08:00"
        result = evaluate(c)
        self.assertFalse(result["events"])
        self.assertIn("post_plan_minutes_insufficient", str(result["blockers"]))
        c["plan"]["rules"]["first_buy"]["created_at"] = "2026-09-23T11:00:00+08:00"
        self.assertIn("action_rule_not_yet_available", str(evaluate(c)["blockers"]))

    def test_time_exit_needs_failed_recovery(self):
        c = fixture("exit")
        rule = c["plan"]["rules"]["exit"]
        rule.update(kind="time_exit", deadline="2026-09-23T10:03:00+08:00")
        self.assertIn("time_exit_failure_condition_missing", str(evaluate(c)["blockers"]))
        rule["failure_condition"] = "not_reclaimed_reference"
        self.assertEqual(len(evaluate(arm(c))["events"]), 1)
        rule["reference"] = 10
        self.assertFalse(evaluate(c)["events"])

    def test_blocked_risk_exit_still_suppresses_a_valid_buy(self):
        c = fixture("add")
        # Time-based failure to recover a higher level can coexist with a lower
        # entry structure; lack of a bid must not make that entry permissible.
        c["plan"]["rules"]["exit"] = dict(basis="completed_1m", version="m1", kind="time_exit",
            confirmation_minutes=3, reference=11, deadline="2026-09-23T10:03:00+08:00",
            failure_condition="not_reclaimed_reference")
        c = arm(c)
        c["quote"]["bid_available"] = False
        result = evaluate(c)
        self.assertFalse(result["events"])
        self.assertFalse(result["confirmed_candidates"])
        exit_row = next(v for v in result["coverage"] if v["action"] == "exit")
        self.assertTrue(exit_row["risk_condition_active"])
        self.assertFalse(result["states"]["add"]["consumed"])
        c["previous_states"] = result["states"]
        c["plan"]["rules"].pop("exit")
        self.assertEqual(evaluate(c)["events"][0]["action"], "add")

    def test_no_add_cannot_be_bypassed_with_t_first_buy(self):
        c = arm(fixture("t_buy_first"))
        c["plan"]["allow_add"] = False
        self.assertIn("temporary_add_forbidden", str(evaluate(c)["blockers"]))

    def test_invalidated_action_requires_new_plan_or_explicit_reset(self):
        c = arm(fixture())
        c["quote"]["price"] = 10.6
        c["minutes"][-1].update(close=10.6, high=10.7)
        invalid = evaluate(c)
        self.assertEqual(invalid["states"]["first_buy"]["state"], "invalidated")
        c = fixture()
        c["previous_states"] = invalid["states"]
        self.assertIn("plan_action_previously_invalidated", str(evaluate(c)["blockers"]))
        c["plan"]["reset_token"] = "explicit-new-observation"
        self.assertEqual(evaluate(c)["states"]["first_buy"]["state"], "confirmed")
        self.assertFalse(evaluate(c)["events"])

    def test_second_leg_transition_and_new_partial_first_tranche(self):
        c = fixture("t_buy_first")
        c["t_episode"] = episode()
        waiting = evaluate(c)
        c["previous_states"] = waiting["states"]
        c["t_episode"]["fills"] = [fill(qty=100)]
        c["quote"]["price"] = 10.9
        for row in c["minutes"]:
            row.update(close=10.9, high=11, low=10.8, vwap=10.5)
        first = evaluate(c)
        self.assertEqual(first["events"][0]["quantity"], 100)
        c["previous_states"] = first["states"]
        self.assertFalse(evaluate(c)["events"])
        c["t_episode"]["fills"].append(fill("later-partial", qty=200))
        second = evaluate(c)
        self.assertEqual(second["events"][0]["quantity"], 200)
        self.assertNotEqual(first["events"][0]["event_key"], second["events"][0]["event_key"])

    def test_baseline_exposes_audit_candidate_not_notification(self):
        result = evaluate(fixture())
        self.assertEqual(len(result["confirmed_candidates"]), 1)
        self.assertFalse(result["events"])

    def test_exit_does_not_require_recommendation_rank(self):
        c = arm(fixture("exit"))
        c["plan"].pop("source_rank")
        self.assertEqual(len(evaluate(c)["events"]), 1)

    def test_restore_sold_position_without_creating_new_net_shares(self):
        c = fixture("t_sell_first")
        c["account"].update(held_quantity=0, sellable_quantity=0, position_risk=0)
        c["t_episode"] = episode("t_sell_first")
        c["t_episode"]["fills"] = [fill(action="t_sell_first", qty=300)]
        c["quote"]["price"] = 9.8
        for row in c["minutes"]:
            row.update(close=9.8, low=9.7, high=10, vwap=9.7)
        result = evaluate(arm(c))
        self.assertEqual(result["events"][0]["quantity"], 300)
        c["plan"]["allow_buy"] = False
        self.assertFalse(evaluate(c)["events"])


class EpisodeTests(unittest.TestCase):
    def test_partial_fills_restart_and_complete(self):
        e = reduce_episode(episode(), [fill()], "2026-09-23T10:04:00+08:00")
        self.assertEqual(e["state"], "first_partial")
        repeated = reduce_episode(e, [fill()], "2026-09-23T10:04:00+08:00")
        self.assertEqual(repeated, e)
        e = reduce_episode(e, [fill("fill-2", qty=200)], "2026-09-23T10:04:00+08:00")
        self.assertEqual(e["state"], "awaiting_second")
        e = reduce_episode(e, [fill("fill-3", "second", 100)], "2026-09-23T10:04:00+08:00")
        self.assertEqual(e["open_quantity"], 200)
        e = reduce_episode(e, [fill("fill-4", "second", 200)], "2026-09-23T10:04:00+08:00")
        self.assertEqual(e["state"], "completed")

    def test_not_order_receipt_or_position_delta(self):
        for source in ("order", "read_receipt", "position_delta", "paper"):
            e = reduce_episode(episode(), [{**fill(), "source": source}], "2026-09-23T10:04:00+08:00")
            self.assertEqual(e["first_quantity"], 0)
            self.assertIn("fill_not_actual_verified", e["blockers"])

    def test_one_fill_cannot_support_two_episodes(self):
        e = reduce_episode(episode(), [fill()], "2026-09-23T10:04:00+08:00", claimed_fill_ids=["fill-1"])
        self.assertIn("fill_already_claimed", e["blockers"])

    def test_cannot_sell_unfilled_first_leg_or_oversize(self):
        e = reduce_episode(episode(), [fill("x", "second", 100)], "2026-09-23T10:04:00+08:00")
        self.assertIn("second_fill_exceeds_actual_first", e["blockers"])
        e = reduce_episode(episode(), [fill(qty=400)], "2026-09-23T10:04:00+08:00")
        self.assertIn("first_fill_exceeds_target", e["blockers"])

    def test_overnight_is_open_exposure_not_completion(self):
        e = reduce_episode(episode(), [fill()], "2026-09-24T10:04:00+08:00")
        self.assertEqual(e["state"], "overnight_open")
        self.assertEqual(e["open_quantity"], 100)

    def test_conflicting_duplicate_future_and_wrong_account(self):
        e = reduce_episode(episode(), [fill(), {**fill(), "price": 12}], "2026-09-23T10:04:00+08:00")
        self.assertIn("fill_id_conflict", e["blockers"])
        e = reduce_episode(episode(), [{**fill(), "account_key": "other"}], "2026-09-23T10:04:00+08:00")
        self.assertIn("fill_identity_mismatch", e["blockers"])
        e = reduce_episode(episode(), [fill()], "2026-09-23T09:55:00+08:00")
        self.assertIn("fill_time_invalid", e["blockers"])


if __name__ == "__main__":
    unittest.main()

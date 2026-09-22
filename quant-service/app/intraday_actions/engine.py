"""Deterministic intraday observations with no I/O and no execution authority."""
from __future__ import annotations

from datetime import timedelta, time
from hashlib import sha256
import json
from math import floor, isfinite

from .contracts import ACTIONS, BUY_ACTIONS, T_ACTIONS, PRIORITY, VERSION
from .episodes import timestamp, positive, reduce_episode, SHANGHAI


def _number(value, *, nonnegative=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and isfinite(value) and (value >= 0 if nonnegative else value > 0))


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _age(value, now, maximum):
    at = timestamp(value)
    return at is not None and 0 <= (now - at).total_seconds() <= maximum


def _cost(notional, side, costs):
    return (max(costs["min_commission"], notional * costs["commission_rate"])
            + notional * (costs["transfer_rate"] + costs["slippage_bps"] / 10000
                          + (costs["stamp_tax_sell_rate"] if side == "sell" else 0)))


def _cost_errors(costs, now):
    fields = ("commission_rate", "min_commission", "stamp_tax_sell_rate", "transfer_rate", "slippage_bps")
    if not isinstance(costs, dict) or not costs.get("source") or timestamp(costs.get("as_of")) is None:
        return ["account_costs_missing"]
    if timestamp(costs["as_of"]) > now or any(not _number(costs.get(k), nonnegative=True) for k in fields):
        return ["account_costs_invalid"]
    return []


def _minute_rows(context, now, created, policy):
    quote = context["quote"]
    rows = []
    for row in context.get("minutes") or []:
        if not isinstance(row, dict):
            return [], ["minute_row_invalid"]
        if row.get("completed") is not True:
            continue
        at = timestamp(row.get("end_at"))
        if at is None:
            return [], ["minute_time_missing"]
        if at > now:
            continue
        if at.astimezone(SHANGHAI).date() != now.astimezone(SHANGHAI).date():
            return [], ["minute_wrong_session"]
        local = at.astimezone(SHANGHAI)
        if local.second or local.microsecond or not (time(9, 31) <= local.time() <= time(11, 30) or time(13, 0) <= local.time() <= time(15, 0)):
            return [], ["minute_outside_session"]
        if row.get("source") != quote.get("source"):
            return [], ["minute_source_mismatch"]
        if not positive(row.get("close")):
            return [], ["minute_values_missing"]
        if positive(row.get("low")) and positive(row.get("high")) and not row["low"] <= row["close"] <= row["high"]:
            return [], ["minute_ohlc_invalid"]
        rows.append((at, row))
    if not rows:
        return [], ["completed_minutes_missing"]
    stamps = [t for t, _ in rows]
    if stamps != sorted(stamps) or len(set(stamps)) != len(stamps):
        return [], ["minute_not_strictly_ordered"]
    for a, b in zip(stamps, stamps[1:]):
        lunch = a.astimezone(SHANGHAI).time() == time(11, 30) and b.astimezone(SHANGHAI).time() in (time(13), time(13, 1))
        if b-a != timedelta(minutes=1) and not lunch:
            return [], ["minute_gap"]
    if not _age(stamps[-1], now, policy["max_minute_age_seconds"]):
        return [], ["minutes_stale"]
    if abs(rows[-1][1]["close"] / quote["price"] - 1) * 100 > policy["max_price_deviation_pct"]:
        return [], ["quote_minute_price_mismatch"]
    return [(at, row) for at, row in rows if at > created], []


def _condition(action, rule, rows, context, now, buy, plan):
    if not isinstance(rule, dict):
        return "blocked", ["action_plan_missing"], []
    if rule.get("basis") != "completed_1m":
        return "blocked", ["explicit_minute_basis_required"], []
    if not rule.get("version"):
        return "blocked", ["minute_rule_version_missing"], []
    count = rule.get("confirmation_minutes")
    if not isinstance(count, int) or isinstance(count, bool) or count < 3:
        return "blocked", ["minute_confirmation_count_invalid"], []
    kind = rule.get("kind")
    allowed = ({"breakout", "pullback_reclaim"} if buy else
               {"hard_stop", "time_exit"} if action == "exit" else
               {"breakdown"} if action == "reduce" else {"exhaustion", "breakout"})
    if kind not in allowed:
        return "blocked", ["action_rule_kind_invalid"], []
    if not positive(rule.get("reference")):
        return "blocked", ["minute_reference_missing"], []
    if kind == "hard_stop" and rule["reference"] != plan["hard_stop"]:
        return "blocked", ["hard_stop_must_not_change"], []
    if len(rows) < count:
        return "waiting", ["post_plan_minutes_insufficient"], []
    tail = rows[-count:]
    if any(b[0]-a[0] != timedelta(minutes=1) for a, b in zip(tail, tail[1:])):
        return "waiting", ["confirmation_crosses_session_gap"], []
    bars = [r for _, r in tail]
    ref = rule["reference"]
    if buy:
        if any(not positive(r.get("vwap")) or not _number(r.get("volume"), nonnegative=True) for r in bars):
            return "blocked", ["minute_vwap_or_volume_missing"], []
        for field in ("max_buy_price", "volume_baseline", "min_volume_ratio"):
            if not positive(rule.get(field)):
                return "blocked", [field + "_missing"], []
        if not _number(rule.get("sector_min_change"), nonnegative=True):
            return "blocked", ["sector_threshold_missing"], []
        market = context.get("market") or {}
        if market.get("sector_confirmed") is not True or not market.get("sector_key") or not isinstance(market.get("sector_change_pct"), (int, float)):
            return "blocked", ["sector_confirmation_missing"], []
        if market.get("source") != context["quote"]["source"]:
            return "blocked", ["sector_source_mismatch"], []
        if not _age(market.get("as_of"), now, context["policy"]["max_market_age_seconds"]):
            return "blocked", ["sector_confirmation_stale"], []
        if context["quote"]["price"] > rule["max_buy_price"]:
            return "invalidated", ["chase_cap_exceeded"], []
        if rule["max_buy_price"] < ref:
            return "blocked", ["buy_price_range_invalid"], []
        if context["quote"]["price"] <= plan["hard_stop"]:
            return "invalidated", ["buy_structure_invalidated"], []
        confirmed = (context["quote"]["price"] >= max(ref, bars[-1]["vwap"])
                     and all(r["close"] >= max(ref, r["vwap"]) and r["volume"] >= rule["volume_baseline"] * rule["min_volume_ratio"] for r in bars)
                     and market["sector_change_pct"] >= rule["sector_min_change"])
        if kind == "pullback_reclaim":
            if any(not positive(r.get("low")) for _, r in rows[:-count]):
                return "blocked", ["pullback_low_missing"], []
            confirmed = confirmed and any(r["low"] <= ref for _, r in rows[:-count])
        reasons = ["连续已完成分钟站稳结构与均价线", "分钟量能与同源板块同时确认"]
    elif kind in {"hard_stop", "breakdown"}:
        confirmed = all(r["close"] < ref for r in bars)
        reasons = ["连续已完成分钟跌破原生效风险线" if kind == "hard_stop" else "连续已完成分钟确认结构失效"]
    elif kind == "time_exit":
        deadline = timestamp(rule.get("deadline"))
        if deadline is None:
            return "blocked", ["time_exit_deadline_missing"], []
        if rule.get("failure_condition") != "not_reclaimed_reference":
            return "blocked", ["time_exit_failure_condition_missing"], []
        confirmed = now >= deadline and tail[-1][0] >= deadline and all(r["close"] < ref for r in bars)
        reasons = ["原计划期限已到，连续已完成分钟仍未收复确认线"]
    elif kind == "exhaustion":
        if any(not positive(r.get("high")) for _, r in rows) or any(not positive(r.get("vwap")) for r in bars):
            return "blocked", ["exhaustion_high_or_vwap_missing"], []
        if not positive(rule.get("peak_reference")) or not positive(rule.get("min_pullback_pct")):
            return "blocked", ["exhaustion_rule_missing"], []
        peak = max(r["high"] for _, r in rows)
        confirmed = (context["quote"]["price"] < min(ref, bars[-1]["vwap"])
                     and peak >= rule["peak_reference"]
                     and (peak-bars[-1]["close"]) / peak * 100 >= rule["min_pullback_pct"]
                     and all(r["close"] < min(ref, r["vwap"]) for r in bars)
                     and all(b["close"] < a["close"] for a, b in zip(bars, bars[1:])))
        reasons = ["冲高后回落且连续分钟走弱，非仅因涨幅或集中度"]
    else:
        if any(not positive(r.get("vwap")) for r in bars):
            return "blocked", ["minute_vwap_missing"], []
        confirmed = (context["quote"]["price"] >= max(ref, bars[-1]["vwap"])
                     and all(r["close"] >= max(ref, r["vwap"]) for r in bars))
        reasons = ["实际首腿成交后，连续分钟达到旧仓卖出条件"]
    return ("confirmed" if confirmed else "waiting"), [], reasons


def evaluate(context: dict) -> dict:
    """Evaluate only explicit facts; missing evidence returns named blockers."""
    result = {"version": VERSION, "status": "blocked", "events": [], "blockers": [],
              "coverage": [], "states": {}, "confirmed_candidates": [],
              "live_effect": "none", "orders_authorized": False}
    if not isinstance(context, dict):
        result["blockers"] = [{"action": "all", "code": "context_invalid"}]
        return result
    now = timestamp(context.get("now"))
    plan = context.get("plan") or {}
    quote = context.get("quote") or {}
    policy = context.get("policy") or {}
    account = context.get("account") or {}
    market = context.get("market") or {}
    errors = []
    if now is None:
        errors.append("now_timezone_missing")
    if context.get("account_key") != "citics-primary":
        errors.append("account_binding_invalid")
    if not context.get("symbol") or not context.get("name") or context.get("role") not in {"holding", "recommendation"}:
        errors.append("scope_identity_missing")
    policy_fields = ("max_quote_age_seconds", "max_minute_age_seconds", "max_account_age_seconds", "max_market_age_seconds", "max_price_deviation_pct")
    if any(not positive(policy.get(k)) for k in policy_fields):
        errors.append("freshness_policy_missing")
    if not positive(quote.get("price")) or not quote.get("source"):
        errors.append("quote_missing")
    if now and positive(policy.get("max_quote_age_seconds")) and not _age(quote.get("as_of"), now, policy["max_quote_age_seconds"]):
        errors.append("quote_stale_or_future")
    created, expires = timestamp(plan.get("created_at")), timestamp(plan.get("expires_at"))
    if not plan.get("id") or not plan.get("version") or created is None or expires is None:
        errors.append("plan_identity_or_time_missing")
    elif now and not created <= now < expires:
        errors.append("plan_not_yet_available" if now < created else "plan_expired")
    if plan.get("quality_status") != "accepted":
        errors.append("plan_quality_rejected")
    if not positive(plan.get("hard_stop")):
        errors.append("hard_stop_missing")
    if market.get("session_open") is not True:
        errors.append("market_session_closed_or_unknown")
    if now:
        clock = now.astimezone(SHANGHAI).time()
        if not (time(9, 30) <= clock <= time(11, 30, 59) or time(13) <= clock <= time(15, 0, 59)):
            errors.append("outside_continuous_session")
    rows = []
    if not errors:
        rows, minute_errors = _minute_rows(context, now, created, policy)
        errors.extend(minute_errors)
    previous = context.get("previous_states") or {}
    candidates = []
    active_risks = []
    episode = None
    if context.get("t_episode") and now:
        episode = reduce_episode(context["t_episode"], [], now, claimed_fill_ids=context.get("other_episode_fill_ids") or ())
    for action in ACTIONS:
        blocked = list(errors)
        state, reasons = "blocked", []
        rule = (plan.get("rules") or {}).get(action)
        leg = "first"
        action_rows = rows
        buy = action in BUY_ACTIONS
        current_episode = episode if episode and episode.get("action") == action else None
        if current_episode:
            if current_episode.get("account_key") != context.get("account_key") or current_episode.get("symbol") != context.get("symbol"):
                blocked.append("episode_identity_mismatch")
            blocked.extend(current_episode.get("blockers") or [])
            if current_episode.get("open_quantity", 0) > 0 and not blocked:
                leg = "second"
                buy = action == "t_sell_first"
                rule = (rule or {}).get("second_leg")
                after = timestamp(current_episode.get("first_filled_at"))
                action_rows = [(at, row) for at, row in rows if after is not None and at > after]
            elif current_episode.get("state") in {"completed", "cancelled"}:
                blocked.append("episode_closed")
            elif current_episode.get("first_quantity", 0) == 0:
                blocked.append("first_leg_actual_fill_required")
        # A partially compiled plan may gain a missing action later. That new
        # action must not consume minutes observed before its own availability,
        # while existing exit rules retain their original lifecycle/baseline.
        base_rule = (plan.get("rules") or {}).get(action) or {}
        for declared_rule in (base_rule, rule or {}):
            if "created_at" in declared_rule:
                rule_created = timestamp(declared_rule["created_at"])
                if rule_created is None:
                    blocked.append("action_rule_created_at_invalid")
                elif now and rule_created > now:
                    blocked.append("action_rule_not_yet_available")
                else:
                    action_rows = [(at, row) for at, row in action_rows if at > rule_created]
        tranche = sorted(str(f["fill_id"]) for f in (current_episode or {}).get("fills", []) if f.get("leg") == "first") if leg == "second" else None
        key = _digest([plan.get("id"), plan.get("version"), plan.get("reset_token"), action, leg, current_episode.get("id") if current_episode else None, tranche])
        lifecycle_key = _digest([plan.get("id"), plan.get("version"), plan.get("reset_token"), action, current_episode.get("id") if current_episode else None])
        prior = previous.get(action) or {}
        incremental_second_quantity = None
        if (leg == "second" and prior.get("lifecycle_key") == lifecycle_key
                and prior.get("leg") == "second" and prior.get("consumed")
                and (current_episode or {}).get("first_quantity", 0) > prior.get("first_quantity", 0)):
            # Earlier notifications are NOT fills, and may still be acted on.
            # Reserve their quantity; a new partial first fill authorizes only
            # the additional tranche, never the entire outstanding leg twice.
            incremental_second_quantity = current_episode["first_quantity"] - prior["first_quantity"]
        if (leg == "second" and prior.get("lifecycle_key") == lifecycle_key
                and (prior.get("leg") == "first" or (current_episode or {}).get("first_quantity", 0) > prior.get("first_quantity", 0))):
            prior = {"state": "waiting", "consumed": False, "plan_key": key}
        prior = prior if prior.get("plan_key") == key else {}
        not_applicable = ((action == "first_buy" and context.get("role") != "recommendation")
                          or (action != "first_buy" and context.get("role") != "holding"))
        qty, locked = 0, 0
        if not_applicable:
            state, blocked = "not_applicable", []
        elif prior.get("state") == "invalidated":
            state, blocked = "invalidated", ["plan_action_previously_invalidated"]
        elif not blocked:
            state, blocked, reasons = _condition(action, rule, action_rows, context, now, buy, plan)
            if state == "confirmed":
                if action in {"exit", "reduce"}:
                    active_risks.append(action)
                if not account.get("snapshot_id") or account.get("quantity_verified") is not True:
                    blocked.append("account_quantity_unverified")
                if not _age(account.get("as_of"), now, policy["max_account_age_seconds"]):
                    blocked.append("account_snapshot_stale")
                account_fields = ("held_quantity", "sellable_quantity") + (("total_assets", "stock_market_value", "allocated_buy_cash", "position_risk") if buy else ())
                for field in account_fields:
                    if not _number(account.get(field), nonnegative=True):
                        blocked.append("account_" + field + "_missing")
                if context["role"] == "holding" and plan.get("snapshot_id") != account.get("snapshot_id"):
                    blocked.append("plan_snapshot_mismatch")
                if context["role"] == "recommendation" and (context.get("formal_recommendation") is not True or not plan.get("decision_id")):
                    blocked.append("formal_recommendation_required")
                if context["role"] == "recommendation" and not isinstance(plan.get("source_rank"), int):
                    blocked.append("source_rank_missing")
                if not blocked:
                    held, sellable = account["held_quantity"], account["sellable_quantity"]
                    locked = held - sellable
                    if sellable > held or any(int(q) != q for q in (held, sellable)):
                        blocked.append("account_quantity_inconsistent")
                    if action == "first_buy" and held != 0:
                        blocked.append("first_buy_already_held")
                    if action != "first_buy" and held <= 0 and not (action == "t_sell_first" and leg == "second"):
                        blocked.append("holding_required")
                    if action in T_ACTIONS and (sellable <= 0 and not (action == "t_sell_first" and leg == "second")):
                        blocked.append("no_sellable_old_shares")
                    requested = current_episode["open_quantity"] if leg == "second" else rule.get("quantity")
                    if incremental_second_quantity is not None:
                        requested = min(requested, incremental_second_quantity)
                    elif leg == "second" and prior.get("consumed") and positive(prior.get("tranche_quantity")):
                        requested = min(requested, prior["tranche_quantity"])
                    if action == "exit":
                        requested = held
                    if not positive(requested) or int(requested) != requested:
                        blocked.append("incremental_quantity_missing")
                    if buy:
                        if plan.get("allow_buy") is not True:
                            blocked.append("buy_forbidden")
                        if action == "add" and (plan.get("allow_add") is not True or plan.get("thesis_valid") is not True):
                            blocked.append("add_forbidden_or_thesis_invalid")
                        if action == "t_buy_first" and plan.get("allow_add") is not True:
                            blocked.append("temporary_add_forbidden")
                        if action == "add" and plan.get("forbid_add_below_cost") is True:
                            if not positive(account.get("position_cost")):
                                blocked.append("explicit_add_cost_policy_unverifiable")
                            elif quote["price"] < account["position_cost"]:
                                blocked.append("explicit_add_below_cost_forbidden")
                        if quote.get("ask_available") is not True:
                            blocked.append("no_ask_execution_uncertain")
                        if plan.get("risk_budget_pct") != 0.05:
                            blocked.append("explicit_existing_five_percent_risk_policy_required")
                        if not positive(account.get("lot_size")) or int(account.get("lot_size", 0)) != account.get("lot_size"):
                            blocked.append("board_lot_missing")
                        blocked.extend(_cost_errors(account.get("costs"), now))
                        if not blocked:
                            worst = rule["max_buy_price"]
                            risk_unit = worst - plan["hard_stop"]
                            if risk_unit <= 0:
                                blocked.append("buy_range_stop_invalid")
                            else:
                                cash = account["total_assets"] - account["stock_market_value"] - account["allocated_buy_cash"]
                                risk = account["total_assets"] * plan["risk_budget_pct"] - account["position_risk"]
                                lot = account["lot_size"]
                                qty = max(0, floor(min(requested, cash / worst, risk / risk_unit) / lot) * lot)
                                while qty > 0 and qty * worst + _cost(qty * worst, "buy", account["costs"]) > cash:
                                    qty -= lot
                                if action == "t_buy_first":
                                    qty = min(qty, floor(sellable / lot) * lot)
                                if qty <= 0:
                                    blocked.append("cash_or_stop_risk_insufficient")
                    else:
                        if quote.get("bid_available") is not True:
                            blocked.append("no_bid_execution_uncertain")
                        if not blocked:
                            qty = min(requested, sellable)
                            if qty <= 0:
                                blocked.append("t_plus_one_no_sellable_shares")
                    if action in T_ACTIONS and not blocked:
                        base_rule = plan["rules"][action]
                        blocked.extend(_cost_errors(account.get("costs"), now))
                        if not positive(base_rule.get("target_price")) or not _number(base_rule.get("min_net_profit"), nonnegative=True):
                            blocked.append("t_net_spread_rule_missing")
                        if action == "t_sell_first" and leg == "first" and rule.get("kind") != "exhaustion":
                            blocked.append("sell_first_requires_exhaustion")
                        if action == "t_buy_first" and leg == "first" and rule.get("kind") != "pullback_reclaim":
                            blocked.append("buy_first_requires_pullback_reclaim")
                        second_rule = base_rule.get("second_leg") or {}
                        if (second_rule.get("basis") != "completed_1m" or not second_rule.get("version")
                                or not isinstance(second_rule.get("confirmation_minutes"), int)
                                or second_rule.get("confirmation_minutes", 0) < 3):
                            blocked.append("t_second_leg_minute_contract_missing")
                        if action == "t_sell_first":
                            if not positive(second_rule.get("max_buy_price")):
                                blocked.append("t_repurchase_cap_missing")
                            if leg == "first":
                                if not positive(base_rule.get("min_sell_price")):
                                    blocked.append("t_first_sell_floor_missing")
                                elif quote["price"] < base_rule["min_sell_price"]:
                                    blocked.append("t_first_sell_floor_breached")
                        elif second_rule.get("kind") != "breakout" or not positive(second_rule.get("reference")):
                            blocked.append("t_old_share_sell_floor_missing")
                        if not blocked:
                            # A card permits its whole declared price range.
                            # The low aspirational target/current quote cannot
                            # prove net spread at the worst permitted buy cap.
                            if action == "t_sell_first":
                                buy_price = second_rule["max_buy_price"]
                                sell_price = (current_episode["first_average_price"] if leg == "second"
                                              else base_rule["min_sell_price"])
                            else:
                                buy_price = (current_episode["first_average_price"] if leg == "second"
                                             else base_rule["max_buy_price"])
                                sell_price = second_rule["reference"]
                            net = qty * (sell_price - buy_price) - _cost(qty * buy_price, "buy", account["costs"]) - _cost(qty * sell_price, "sell", account["costs"])
                            if net < base_rule["min_net_profit"]:
                                blocked.append("t_net_spread_insufficient_no_chasing")
                if blocked:
                    state = "blocked"
        coverage = {"action": action, "state": state, "blockers": sorted(set(blocked)), "leg": leg,
                    "risk_condition_active": action in active_risks}
        result["coverage"].append(coverage)
        result["blockers"].extend({"action": action, "code": code} for code in coverage["blockers"])
        consumed = bool(prior.get("consumed"))
        baseline = state == "confirmed" and not prior
        state_entry = {"plan_key": key, "lifecycle_key": lifecycle_key, "leg": leg,
                       "first_quantity": (current_episode or {}).get("first_quantity", 0),
                       "tranche_quantity": qty if state == "confirmed" else prior.get("tranche_quantity"),
                       "state": state, "consumed": consumed, "baseline_suppressed": baseline}
        result["states"][action] = state_entry
        if state == "confirmed":
            event = {"event_key": _digest([key, "confirmed"]), "action": action, "leg": leg,
                     "symbol": context["symbol"], "name": context["name"], "account_key": context["account_key"],
                     "quantity": qty, "locked_quantity": locked, "price": quote["price"],
                     "price_range": [rule["reference"], rule["max_buy_price"]] if buy else None,
                     "reference": rule["reference"], "hard_stop": plan["hard_stop"],
                     "max_buy_price": rule.get("max_buy_price") if buy else None,
                     "min_sell_price": (rule["reference"] if action == "t_buy_first" and leg == "second"
                                        else rule.get("min_sell_price") if action == "t_sell_first" and leg == "first" else None),
                     "evidence_at": action_rows[-1][0].isoformat(), "quote_at": quote["as_of"], "account_at": account["as_of"],
                     "plan_id": plan["id"], "plan_version": plan["version"], "source_rank": plan.get("source_rank"),
                     "decision_id": plan.get("decision_id"), "snapshot_id": account["snapshot_id"],
                     "episode_id": current_episode.get("id") if current_episode else None,
                     "reasons": reasons, "live_effect": "none", "actual_fill": False,
                     "research_cash_only": buy, "notify": not baseline and not consumed}
            candidates.append(event)
    if candidates:
        candidates.sort(key=lambda e: PRIORITY[e["action"]])
        winner = candidates[0]
        risk_winner = min(active_risks, key=lambda a: PRIORITY[a]) if active_risks else None
        suppress_all = risk_winner is not None and PRIORITY[risk_winner] < PRIORITY[winner["action"]]
        winner_action = risk_winner if suppress_all else winner["action"]
        # Even baseline/consumed risk exits suppress buys; cooldown never overrides risk.
        for event in candidates if suppress_all else candidates[1:]:
            for cov in result["coverage"]:
                if cov["action"] == event["action"]:
                    cov["state"] = "suppressed"
                    cov["blockers"].append("higher_priority_action:" + winner_action)
                    result["blockers"].append({"action": event["action"], "code": "higher_priority_action:" + winner_action})
                    result["states"][event["action"]]["state"] = "suppressed"
                    result["states"][event["action"]]["baseline_suppressed"] = False
        if not suppress_all:
            result["states"][winner["action"]]["consumed"] = True
            notify = winner.pop("notify")
            result["confirmed_candidates"].append(dict(winner))
            if notify:
                result["events"].append(winner)
    applicable = [c for c in result["coverage"] if c["state"] != "not_applicable"]
    blocked_count = sum(c["state"] == "blocked" for c in applicable)
    result["status"] = "blocked" if blocked_count == len(applicable) else "partial" if blocked_count else "ready"
    return result

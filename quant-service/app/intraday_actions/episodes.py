"""Pure fill-backed T episode reducer. Caller owns atomic cross-episode claims.

``reduce_episode(episode, fills, now, claimed_fill_ids=())`` never mutates input.
Each fill has fill_id, account_key, symbol, side (buy/sell), leg (first/second),
quantity, price, occurred_at, observed_at, source (broker_import/user_report),
verified=True. A user report must explicitly attest an ACTUAL fill. An order,
read receipt or position delta is not an accepted source. Persist both the
result and unique (account_key, fill_id) claims in one database transaction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0


def reduce_episode(episode: dict, fills: list, now, *, claimed_fill_ids=()) -> dict:
    out = deepcopy(episode)
    blockers = []
    current = timestamp(now)
    created = timestamp(out.get("created_at"))
    required = ("id", "account_key", "symbol", "session_date", "target_quantity", "old_sellable_quantity")
    if any(out.get(k) is None for k in required) or current is None or created is None:
        return {**out, "state": "blocked", "blockers": ["episode_identity_missing"]}
    action = out.get("action")
    target = out["target_quantity"]
    old = out["old_sellable_quantity"]
    if action not in {"t_buy_first", "t_sell_first"} or not positive(target) or not positive(old) or target > old:
        return {**out, "state": "blocked", "blockers": ["episode_quantity_invalid"]}
    if created > current or str(out["session_date"]) != created.astimezone(SHANGHAI).date().isoformat():
        return {**out, "state": "blocked", "blockers": ["episode_time_invalid"]}
    existing = list(out.get("fills") or [])
    accepted = []
    seen = {}
    claimed = set(claimed_fill_ids)
    first_side = "buy" if action == "t_buy_first" else "sell"
    for fill in existing + list(fills):
        fid = fill.get("fill_id")
        if fid in seen:
            if seen[fid] != fill:
                blockers.append("fill_id_conflict")
            continue
        if not fid or fid in claimed:
            blockers.append("fill_already_claimed" if fid else "fill_id_missing")
            continue
        at, observed = timestamp(fill.get("occurred_at")), timestamp(fill.get("observed_at"))
        leg = fill.get("leg")
        if fill.get("source") not in {"broker_import", "user_report"} or fill.get("verified") is not True:
            blockers.append("fill_not_actual_verified")
            continue
        if fill.get("account_key") != out["account_key"] or fill.get("symbol") != out["symbol"]:
            blockers.append("fill_identity_mismatch")
            continue
        if (at is None or observed is None or not created <= at <= observed <= current
                or at.astimezone(SHANGHAI).date().isoformat() != str(out["session_date"])):
            blockers.append("fill_time_invalid")
            continue
        if leg not in {"first", "second"} or fill.get("side") != (first_side if leg == "first" else ("sell" if first_side == "buy" else "buy")):
            blockers.append("fill_leg_side_mismatch")
            continue
        if not positive(fill.get("quantity")) or int(fill["quantity"]) != fill["quantity"] or not positive(fill.get("price")):
            blockers.append("fill_values_invalid")
            continue
        seen[fid] = fill
        accepted.append(deepcopy(fill))
    accepted.sort(key=lambda f: (timestamp(f["occurred_at"]), f["leg"] != "first", str(f["fill_id"])))
    first_qty = second_qty = 0
    valid = []
    for fill in accepted:
        qty = fill["quantity"]
        if fill["leg"] == "first":
            if first_qty + qty > target:
                blockers.append("first_fill_exceeds_target")
                continue
            first_qty += qty
        else:
            if second_qty + qty > first_qty:
                blockers.append("second_fill_exceeds_actual_first")
                continue
            second_qty += qty
        valid.append(fill)
    first = [f for f in valid if f["leg"] == "first"]
    open_qty = first_qty - second_qty
    state = ("completed" if first_qty == target and open_qty == 0 else
             "partially_completed" if second_qty else "awaiting_second" if first_qty == target else
             "first_partial" if first_qty else "awaiting_first_fill")
    if str(out["session_date"]) != current.astimezone(SHANGHAI).date().isoformat() and open_qty:
        state = "overnight_open"
        blockers.append("episode_overnight_exposure")
    if out.get("cancelled") is True:
        state = "cancelled"
        if open_qty:
            blockers.append("cancelled_episode_open_exposure")
    if blockers and state not in {"overnight_open", "cancelled"}:
        state = "blocked"
    out.update(fills=valid, state=state, first_quantity=first_qty,
               second_quantity=second_qty, open_quantity=open_qty,
               first_average_price=(sum(f["quantity"] * f["price"] for f in first) / first_qty if first_qty else None),
               first_filled_at=(max(timestamp(f["occurred_at"]) for f in first).isoformat() if first else None),
               consumed_fill_ids=[f["fill_id"] for f in valid], blockers=sorted(set(blockers)))
    return out

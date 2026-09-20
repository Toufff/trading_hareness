"""Shared, deterministic selector for a stock's immutable discovery origin."""
from __future__ import annotations

from typing import Any

SELECTOR_VERSION = "primary_origin_v1"
STAGE_LANE = {
    "accumulation": "accumulation",
    "initial_breakout": "expansion",
    "strong_pullback": "pullback",
    "post_limit": "relay",
}


def _key(membership: dict[str, Any]) -> tuple[int, str, str]:
    rank = membership.get("rank")
    return (rank if isinstance(rank, int) and rank > 0 else 2**31 - 1,
            str(membership.get("lane") or ""), str(membership.get("origin_id") or ""))


def select_primary_origin(item: dict[str, Any], *, stage: str | None = None,
                          context: str = "recommendation") -> dict[str, Any]:
    """Select ownership, never a cross-lane score or a current-stage rewrite.

    An explicit binding is authoritative and a broken binding fails closed.
    Otherwise the approved stage preference is applied before a stable
    ``(rank, lane, origin_id)`` fallback.  Callers persist this result; later
    stage changes may choose an evaluation lane but do not rewrite it.
    """
    memberships = [dict(value) for value in item.get("memberships") or []]
    explicit = item.get("primary_origin_id")
    if explicit:
        matches = [m for m in memberships if m.get("origin_id") == explicit]
        if len(matches) != 1:
            raise ValueError("primary_origin_binding_conflict")
        selected, reason = matches[0], "explicit_primary_origin_id"
    else:
        preferred = STAGE_LANE.get(stage or item.get("stage"))
        candidates = [m for m in memberships if m.get("lane") == preferred] if preferred else []
        if candidates:
            selected, reason = min(candidates, key=_key), "approved_stage_mapping"
        elif memberships:
            selected, reason = min(memberships, key=_key), "deterministic_membership_fallback"
        elif preferred:
            selected, reason = {"lane": preferred, "origin_id": None}, "stage_without_membership"
        else:
            raise ValueError("recommended_stock_has_no_current_strategy_comparison")
    if not isinstance(selected.get("lane"), str) or not selected["lane"].strip():
        raise ValueError("primary_origin_lane_missing")
    return {"origin_id": selected.get("origin_id"), "lane": selected.get("lane"),
            "reason": reason, "selector_version": SELECTOR_VERSION, "context": context}

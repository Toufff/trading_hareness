"""Shadow research rule distilled from a ten-session ranking playbook.

The rule composes four contemporaneous facts without creating an order:

1. a symbol remains inside its board's ten-session top-30 ranking;
2. the market cycle is supportive at ``strategy_available_at``;
3. an exactly mapped leader or peer group supplies the external force;
4. current price/volume evidence remains accepted above session VWAP.

The workbook that motivated this rule is a retrospective decision journal, not
an execution ledger.  Consequently every positive state is shadow evidence and
``decision_eligible`` is always false until a separate replay and promotion
record exists.
"""

from __future__ import annotations

import math
from typing import Any


MODEL_VERSION = "ten-day-leader-vwap-coordination-shadow-v1"
TOP_RANK_LIMIT = 30
BOARD_EXPANSION_MIN_PCT = {"main": 5.0, "growth": 10.0, "bj": 15.0}
SUPPORTIVE_CYCLE_STATES = {"attack_incubating", "attack_accelerating", "repair", "handoff"}
RISK_CYCLE_STATES = {"kill_high", "retreat", "panic", "ice_point"}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _payload(
    state: str,
    *,
    candidate_path: str | None,
    shadow_eligible: bool,
    reason_codes: list[str],
    risk_flags: list[str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model_version": MODEL_VERSION,
        "scope": "research_only_no_orders",
        "shadow_state": state,
        "shadow_eligible": shadow_eligible,
        "decision_eligible": False,
        "candidate_path": candidate_path,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "risk_flags": list(dict.fromkeys([
            "retrospective_workbook_distillation",
            "requires_point_in_time_replay",
            "manual_review_required",
            "no_automatic_order",
            *risk_flags,
        ])),
        "evidence": evidence,
    }


def classify_ten_day_coordination(
    candidate: dict[str, Any],
    cycle_context: dict[str, Any] | None,
    minute_features: dict[str, Any] | None,
    peer_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify the playbook's leader/coordination/VWAP state causally.

    The board names intentionally match the workbook taxonomy: ``main`` for
    the 10% boards, ``growth`` for ChiNext/STAR, and ``bj`` for Beijing Stock
    Exchange.  The 5/10/15 percent cut-offs reproduce its candidate columns;
    they are discovery thresholds, not expected returns or entry prices.
    """
    cycle = cycle_context or {}
    minute = minute_features or {}
    peers = peer_context or {}
    symbol = str(candidate.get("symbol") or "").upper()
    board = str(candidate.get("board") or "")
    rank = _number(candidate.get("ten_day_rank"))
    return_10d = _number(candidate.get("ten_day_return_pct"))
    current_return = _number(candidate.get("current_return_pct"))
    threshold = BOARD_EXPANSION_MIN_PCT.get(board)
    cycle_state = str(cycle.get("state") or "unavailable")
    above_vwap = _number(minute.get("above_vwap_pct"))
    return_3m = _number(minute.get("return_3m_pct"))
    volume_multiple = _number(minute.get("minute_volume_multiple"))
    available_peers = int(_number(peers.get("available_peer_count")) or 0)
    confirming_peers = int(_number(peers.get("confirming_peer_count")) or 0)
    peer_breadth = _number(peers.get("confirming_breadth")) or 0.0
    exact_mapping = bool(peers.get("exact_sector_mapping"))
    leader_limit_up = bool(peers.get("leader_limit_up"))
    is_limit_up = bool(candidate.get("is_limit_up"))
    evidence = {
        "symbol": symbol or None,
        "board": board or None,
        "ten_day_rank": int(rank) if rank is not None else None,
        "ten_day_return_pct": return_10d,
        "current_return_pct": current_return,
        "board_expansion_min_pct": threshold,
        "cycle_state": cycle_state,
        "strategy_available_at": cycle.get("strategy_available_at"),
        "external_force": {
            "exact_sector_mapping": exact_mapping,
            "leader_limit_up": leader_limit_up,
            "available_peer_count": available_peers,
            "confirming_peer_count": confirming_peers,
            "confirming_breadth": round(peer_breadth, 4),
        },
        "internal_force": {
            "return_3m_pct": return_3m,
            "above_vwap_pct": above_vwap,
            "minute_volume_multiple": volume_multiple,
        },
    }

    if not symbol or threshold is None or rank is None or current_return is None:
        return _payload(
            "data_unavailable", candidate_path=None, shadow_eligible=False,
            reason_codes=["missing_candidate_identity_or_ranking"],
            risk_flags=["candidate_inputs_incomplete"], evidence=evidence,
        )
    if rank < 1 or rank > TOP_RANK_LIMIT:
        return _payload(
            "outside_ten_day_top_30", candidate_path=None, shadow_eligible=False,
            reason_codes=["outside_board_top_30"], risk_flags=[], evidence=evidence,
        )

    if return_10d is not None and return_10d >= 100:
        candidate_path = "ten_day_leader"
    elif is_limit_up:
        candidate_path = "ranked_limit_continuation"
    elif current_return >= threshold:
        candidate_path = "ranked_expansion"
    else:
        candidate_path = "ranked_observation"

    if candidate_path == "ranked_observation":
        return _payload(
            "ranked_but_not_expanding", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["below_board_expansion_threshold"], risk_flags=[], evidence=evidence,
        )
    if not cycle.get("strategy_available_at"):
        return _payload(
            "cycle_context_unavailable", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["strategy_available_at_missing"], risk_flags=["point_in_time_cycle_missing"], evidence=evidence,
        )
    if cycle_state in RISK_CYCLE_STATES or cycle_state not in SUPPORTIVE_CYCLE_STATES:
        return _payload(
            "cycle_risk_blocked", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["cycle_not_supportive"], risk_flags=[f"cycle_{cycle_state}"], evidence=evidence,
        )
    if above_vwap is not None and return_3m is not None and above_vwap <= -0.15 and return_3m <= -0.50:
        return _payload(
            "acceptance_failure", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["vwap_acceptance_lost", "negative_short_horizon_momentum"],
            risk_flags=["workbook_exit_condition_observed"], evidence=evidence,
        )
    if not exact_mapping:
        return _payload(
            "external_force_unavailable", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["exact_sector_mapping_missing"], risk_flags=["exact_sector_mapping_missing"], evidence=evidence,
        )
    external_confirmed = leader_limit_up or (
        available_peers >= 2 and confirming_peers >= 2 and peer_breadth >= 0.50
    )
    if not external_confirmed:
        return _payload(
            "external_force_unconfirmed", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["leader_or_peer_coordination_missing"], risk_flags=[], evidence=evidence,
        )
    if above_vwap is None or return_3m is None or volume_multiple is None:
        return _payload(
            "internal_force_unavailable", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["minute_vwap_or_volume_missing"], risk_flags=["minute_evidence_incomplete"], evidence=evidence,
        )
    internal_confirmed = above_vwap >= 0 and return_3m >= 0.50 and volume_multiple >= 1.50
    if not internal_confirmed:
        return _payload(
            "internal_force_unconfirmed", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["vwap_momentum_volume_not_jointly_confirmed"], risk_flags=[], evidence=evidence,
        )
    if bool(candidate.get("is_one_word_board")):
        return _payload(
            "one_word_board_observation", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["unavailable_low_cost_entry"], risk_flags=["one_word_board_not_entry"], evidence=evidence,
        )
    if bool(candidate.get("recently_suspended")):
        return _payload(
            "recent_suspension_observation", candidate_path=candidate_path, shadow_eligible=False,
            reason_codes=["recent_suspension_requires_manual_review"], risk_flags=["recent_suspension"], evidence=evidence,
        )
    return _payload(
        "confirmed_coordination", candidate_path=candidate_path, shadow_eligible=True,
        reason_codes=["ten_day_rank", "supportive_cycle", "external_force", "internal_vwap_acceptance"],
        risk_flags=["shadow_sample_only"], evidence=evidence,
    )


__all__ = [
    "BOARD_EXPANSION_MIN_PCT",
    "MODEL_VERSION",
    "RISK_CYCLE_STATES",
    "SUPPORTIVE_CYCLE_STATES",
    "TOP_RANK_LIMIT",
    "classify_ten_day_coordination",
]

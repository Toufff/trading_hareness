"""Pure intraday confirmation for the ten-day leader shadow cohort.

The post-close ranking stays immutable.  This module only projects the
currently observed quote, minute and exact-peer evidence onto that ranked
cohort, and always retains the research-only decision boundary.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Callable

from .ten_day_leader_rotation_research import classify_ten_day_coordination


INTRADAY_ROTATION_LIMIT = 20
INTRADAY_ROTATION_SECONDS = 45


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def select_intraday_rotation_slice(candidates: list[dict[str, Any]], observed_at: datetime,
                                   *, limit: int = INTRADAY_ROTATION_LIMIT) -> tuple[list[dict[str, Any]], int]:
    """Select a stable, fair bounded minute-evidence slice from the 90 names."""
    ordered = sorted(
        candidates,
        key=lambda item: (str(item.get("board") or ""), int(item.get("board_rank") or 99), str(item.get("symbol") or "")),
    )
    if not ordered:
        return [], 0
    bounded = max(1, min(int(limit), len(ordered)))
    rotation = int(observed_at.timestamp() // INTRADAY_ROTATION_SECONDS)
    offset = (rotation * bounded) % len(ordered)
    return (ordered[offset:] + ordered[:offset])[:bounded], offset


def intraday_rotation_due(observed_at: datetime) -> bool:
    """Run one minute-evidence batch once per wall-clock minute without state."""
    # The normal scanner can be phase-aligned at ``:05`` / ``:35``.  Use a
    # ten-second boundary window so it still contributes once per minute.
    return observed_at.second >= 55 or observed_at.second < 10


def evaluate_intraday_rotation_candidates(
    *,
    run: dict[str, Any],
    candidates: list[dict[str, Any]],
    observed_at: datetime,
    quotes: dict[str, dict[str, Any]],
    minute_features: dict[str, dict[str, Any]],
    peer_contexts: dict[str, dict[str, Any]],
    market_contexts: dict[str, dict[str, Any]],
    quote_source: Callable[[dict[str, Any] | None], str],
) -> list[dict[str, Any]]:
    """Evaluate selected candidates from causal, already-observed inputs only."""
    strategy_available_at = run.get("strategy_available_at")
    observations: list[dict[str, Any]] = []
    for stored in candidates:
        candidate = dict(stored)
        symbol = str(candidate.get("symbol") or "").upper()
        quote = quotes.get(symbol)
        # The durable candidate projection names this board-local ten-session
        # rank ``board_rank``; the pure workbook rule accepts ``ten_day_rank``.
        candidate["ten_day_rank"] = candidate.get("ten_day_rank", candidate.get("board_rank"))
        # Never carry a prior close's return into a live confirmation.
        candidate["current_return_pct"] = _number((quote or {}).get("pct_change"))
        minute = minute_features.get(symbol)
        market_context = market_contexts.get(symbol) or {}
        peers = dict(peer_contexts.get(symbol) or {})
        groups = peers.get("exact_membership_groups") if isinstance(peers.get("exact_membership_groups"), list) else []
        peers["exact_sector_mapping"] = bool(groups)
        # A board peer must be observed in this same bounded slice before it
        # contributes to breadth.  Do not infer a leader from a board label.
        peers["leader_limit_up"] = False
        cycle = {
            "state": market_context.get("market_state") or "unavailable",
            "strategy_available_at": strategy_available_at,
        }
        result = classify_ten_day_coordination(candidate, cycle, minute, peers)
        observations.append({
            "symbol": symbol,
            "observed_at": observed_at,
            "quote_source": quote_source(quote),
            "shadow_state": result["shadow_state"],
            "shadow_eligible": bool(result["shadow_eligible"]),
            "decision_eligible": False,
            "evidence": result["evidence"],
            "reason_codes": result["reason_codes"],
            "risk_flags": result["risk_flags"],
            "source_snapshot": {
                "quote": quote or {"status": "missing"},
                "minute_features": minute or {"status": "missing_or_not_in_rotation_slice"},
                "peer_context": peers,
                "market_context": market_context,
                "rotation_scope": "bounded_top30_per_board_shadow_cohort",
            },
        })
    return observations


__all__ = [
    "INTRADAY_ROTATION_LIMIT", "INTRADAY_ROTATION_SECONDS",
    "evaluate_intraday_rotation_candidates", "intraday_rotation_due", "select_intraday_rotation_slice",
]

"""Orchestration for the decoupled ten-day leader-rotation shadow run."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import hashlib
from typing import Any, Callable

from .ten_day_leader_rotation_research import MODEL_VERSION


@dataclass(frozen=True)
class TenDayLeaderRotationDependencies:
    latest_full_market_date: Callable[[int], date | None]
    load_inputs: Callable[[date], Any]
    rank_candidates: Callable[..., dict[str, Any]]
    classify: Callable[..., dict[str, Any]]
    persist: Callable[..., Any]
    json_safe: Callable[[Any], Any]


def run_ten_day_leader_rotation(request: Any, dependencies: TenDayLeaderRotationDependencies) -> dict[str, Any]:
    """Build and persist a daily candidate pool using stored evidence only."""
    as_of_date = request.as_of_date or dependencies.latest_full_market_date(request.minimum_full_market_symbols)
    if as_of_date is None:
        return {
            "status": "blocked", "reason": "no_full_market_daily_date", "as_of_date": None,
            "scope": "research_only_no_orders", "candidates": [],
        }
    inputs = dependencies.load_inputs(as_of_date)
    required_coverage = (inputs.expected_daily_symbols * 95 + 99) // 100
    if inputs.expected_daily_symbols and inputs.daily_symbols < required_coverage:
        return {
            "status": "blocked", "reason": "incomplete_full_market_daily_coverage",
            "as_of_date": str(as_of_date), "scope": "research_only_no_orders", "candidates": [],
            "source_status": {
                "daily_symbols": inputs.daily_symbols,
                "expected_daily_symbols": inputs.expected_daily_symbols,
                "minimum_required_symbols": required_coverage,
            },
        }
    ranked = dependencies.rank_candidates(
        inputs.daily_rows, as_of_date, daily_symbols=inputs.daily_symbols,
        minimum_full_market_symbols=request.minimum_full_market_symbols,
        per_board_limit=request.per_board_limit,
    )
    candidates = []
    for candidate in ranked.get("candidates") or []:
        # Context interfaces are deliberately absent from this post-close
        # materializer.  A later intraday evaluator can supply cycle, minute and
        # peer projections without changing ranking or persistence ownership.
        candidates.append({**candidate, **dependencies.classify(candidate, None, None, None)})
    state_counts = Counter(str(item.get("shadow_state") or "unknown") for item in candidates)
    summary = {
        "candidate_count": len(candidates),
        "shadow_eligible_count": sum(bool(item.get("shadow_eligible")) for item in candidates),
        "decision_eligible_count": sum(bool(item.get("decision_eligible")) for item in candidates),
        "shadow_state_counts": dict(sorted(state_counts.items())),
        "phase": "post_close_ranking_awaiting_intraday_context",
        "reason": ranked.get("reason"),
    }
    run_key = hashlib.sha256(f"{MODEL_VERSION}:{as_of_date}".encode()).hexdigest()
    run_id = dependencies.persist(
        run_key=run_key, as_of_date=as_of_date, strategy_available_at=inputs.strategy_available_at,
        model_version=MODEL_VERSION, status=ranked["status"], source_status=ranked.get("source_status") or {},
        summary=summary, candidates=candidates, json_safe=dependencies.json_safe,
    )
    return {
        "run_id": str(run_id), "run_key": run_key, "model_version": MODEL_VERSION,
        "as_of_date": str(as_of_date), "strategy_available_at": (
            inputs.strategy_available_at.isoformat() if inputs.strategy_available_at else None
        ),
        "status": ranked["status"], "reason": ranked.get("reason"),
        "scope": "research_only_no_orders", "source_status": ranked.get("source_status") or {},
        "summary": summary, "candidates": candidates,
    }


__all__ = ["TenDayLeaderRotationDependencies", "run_ten_day_leader_rotation"]

"""Deterministic post-close candidate screening over caller-owned evidence."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable


def screen_candidates(
    as_of_date: date,
    limit: int,
    minimum_full_market_symbols: int,
    coverage_symbols: int,
    rows: list[dict[str, Any]],
    board_contexts: dict[str, dict[str, Any]],
    *,
    daily_base_structure: Callable[[list[dict[str, Any]]], dict[str, Any]],
    forming_structure: Callable[[list[dict[str, Any]]], dict[str, Any]],
    fresh_start_structure: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    """Screen already-persisted daily evidence without provider or DB access."""
    if coverage_symbols < minimum_full_market_symbols:
        return {
            "status": "blocked", "as_of_date": str(as_of_date), "candidates": [], "screen_observations": [],
            "reason": f"only {coverage_symbols} symbols have saved daily bars; need {minimum_full_market_symbols}",
            "source_status": {"daily_symbols": coverage_symbols,
                               "minimum_full_market_symbols": minimum_full_market_symbols},
        }

    grouped: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str | None] = {}
    for row in rows:
        item = dict(row)
        symbol = str(item["symbol"])
        grouped.setdefault(symbol, []).append(item)
        names[symbol] = item.get("name")

    proposals: dict[str, dict[str, Any]] = {}
    screen_observations: list[dict[str, Any]] = []
    strict_ready = provisional_ready = fresh_started = 0
    for symbol, bars in grouped.items():
        board = board_contexts.get(symbol)
        board_positive = bool(board and float(board.get("net_amount") or 0) > 0)
        board_bonus = (12 * float((board or {}).get("flow_percentile") or 0)
                       if board_positive else (-12 if board else 0))
        risk_flags = [] if board else ["no_exact_ths_concept_mapping"]
        if board and not board_positive:
            risk_flags.append("nonpositive_exact_board_flow")
        evaluated_structures: dict[str, dict[str, Any]] = {}
        proposals_for_symbol: list[dict[str, Any]] = []
        if len(bars) >= 30:
            structure = daily_base_structure(bars[-30:])
            evaluated_structures["base_ready_30d"] = structure
            risk_flags = [*risk_flags, *list(structure.get("quality_flags") or [])]
            if structure.get("status") == "ready":
                strict_ready += 1
                score = min(100.0, float(structure.get("score") or 0) * 0.88 + board_bonus)
                proposals_for_symbol.append({
                    "symbol": symbol, "name": names.get(symbol), "candidate_type": "base_ready_30d",
                    "score": round(score, 2), "structure": structure,
                    "board_context": board or {"exact_member_mapping": False}, "risk_flags": risk_flags,
                })
        elif len(bars) >= 15:
            structure = forming_structure(bars)
            evaluated_structures["base_forming_15d"] = structure
            risk_flags = [*risk_flags, *list(structure.get("quality_flags") or [])]
            if structure.get("status") == "forming":
                provisional_ready += 1
                score = min(100.0, float(structure.get("score") or 0) * 0.82 + board_bonus)
                proposals_for_symbol.append({
                    "symbol": symbol, "name": names.get(symbol), "candidate_type": "base_forming_15d",
                    "score": round(score, 2), "structure": structure,
                    "board_context": board or {"exact_member_mapping": False},
                    "risk_flags": [*risk_flags, "provisional_15_session_structure"],
                })
        if len(bars) >= 15:
            started = fresh_start_structure(bars)
            evaluated_structures["fresh_start_15d"] = started
            risk_flags = [*risk_flags, *list(started.get("quality_flags") or [])]
            if started.get("status") == "started":
                fresh_started += 1
                score = min(100.0, float(started.get("score") or 0) * 0.78 + board_bonus)
                proposals_for_symbol.append({
                    "symbol": symbol, "name": names.get(symbol), "candidate_type": "fresh_start_15d",
                    "score": round(score, 2), "structure": started,
                    "board_context": board or {"exact_member_mapping": False},
                    "risk_flags": [*risk_flags, "provisional_15_session_structure"],
                })
        if proposals_for_symbol:
            priority = {"base_ready_30d": 3, "base_forming_15d": 2, "fresh_start_15d": 1}
            selected = max(
                proposals_for_symbol,
                key=lambda item: (float(item["score"]), priority[item["candidate_type"]]),
            )
            proposals[symbol] = selected
            screen_observations.append({
                "symbol": symbol, "name": names.get(symbol), "screen_state": "candidate",
                "candidate_type": selected["candidate_type"], "score": selected["score"],
                "reason_codes": selected["risk_flags"], "structure": selected["structure"],
                "board_context": selected["board_context"],
            })
        else:
            reason_codes = ["insufficient_daily_history"] if len(bars) < 15 else ["no_post_close_structure_matched"]
            if not board:
                reason_codes.append("no_exact_ths_concept_mapping")
            for structure in evaluated_structures.values():
                reason_codes.extend(str(flag) for flag in structure.get("quality_flags") or [])
            screen_observations.append({
                "symbol": symbol, "name": names.get(symbol),
                "screen_state": "insufficient_history" if len(bars) < 15 else "rejected",
                "candidate_type": None, "score": None,
                "reason_codes": list(dict.fromkeys(reason_codes)),
                "structure": evaluated_structures,
                "board_context": board or {"exact_member_mapping": False},
            })

    candidates = sorted(
        proposals.values(),
        key=lambda item: (item["candidate_type"] != "base_ready_30d", -float(item["score"]), item["symbol"]),
    )[:limit]
    return {
        "status": "completed", "as_of_date": str(as_of_date), "candidates": candidates,
        "screen_observations": screen_observations,
        "source_status": {
            "daily_symbols": coverage_symbols, "daily_bars": len(rows),
            "symbols_with_30_sessions": sum(1 for bars in grouped.values() if len(bars) >= 30),
            "symbols_with_15_sessions": sum(1 for bars in grouped.values() if len(bars) >= 15),
            "exact_board_context_symbols": len(board_contexts),
            "screened_symbols": len(grouped), "screen_observation_count": len(screen_observations),
        },
        "summary": {"base_ready_30d": strict_ready, "base_forming_15d": provisional_ready,
                    "fresh_start_15d": fresh_started, "eligible_candidates": len(proposals), "returned": len(candidates)},
        "notice": "盘后研究候选池：不自动加观察、不自动下单；15日结构仅为历史尚在积累期的暂定观察。",
    }


__all__ = ["screen_candidates"]

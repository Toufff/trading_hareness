"""Deterministic post-close candidate screening over caller-owned evidence."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _percentiles(values: dict[str, float | None]) -> dict[str, float]:
    ranked = sorted((value, symbol) for symbol, value in values.items() if value is not None and value >= 0)
    if not ranked:
        return {symbol: 0.5 for symbol in values}
    denominator = max(1, len(ranked) - 1)
    result = {symbol: index / denominator for index, (_value, symbol) in enumerate(ranked)}
    return {symbol: result.get(symbol, 0.5) for symbol in values}


def _structure_quality(structure: dict[str, Any]) -> float:
    metrics = structure.get("metrics") if isinstance(structure, dict) else None
    if not isinstance(metrics, dict):
        return _clamp((_number(structure.get("score")) or 50.0) / 100)
    recent_range = _number(metrics.get("recent_range_pct"))
    old_volatility = _number(metrics.get("old_volatility"))
    recent_volatility = _number(metrics.get("recent_volatility"))
    volume_dry = _number(metrics.get("volume_dry_up_ratio"))
    support_tests = _number(metrics.get("support_tests"))
    close_to_resistance = _number(metrics.get("close_to_resistance_pct"))
    range_component = 0.5 if recent_range is None else _clamp((10.0 - recent_range) / 8.0)
    volatility_component = (
        0.5 if old_volatility is None or recent_volatility is None or old_volatility <= 0
        else _clamp(1.0 - recent_volatility / old_volatility)
    )
    volume_component = 0.5 if volume_dry is None else _clamp((1.10 - volume_dry) / 0.55)
    support_component = 0.5 if support_tests is None else _clamp(support_tests / 10.0)
    resistance_component = 0.5 if close_to_resistance is None else _clamp(1.0 - close_to_resistance / 7.0)
    return (
        range_component * 0.25 + volatility_component * 0.25 + volume_component * 0.20
        + support_component * 0.15 + resistance_component * 0.15
    )


def _ranking_score(
    structure: dict[str, Any], latest: dict[str, Any], board: dict[str, Any] | None,
    liquidity_percentile: float,
) -> tuple[float, dict[str, float], list[str]]:
    amount = _number(latest.get("amount"))
    turnover = _number(latest.get("turnover_rate"))
    volume_ratio = _number(latest.get("volume_ratio"))
    main_net = _number(latest.get("main_net_amount"))
    structure_component = _structure_quality(structure)
    board_component = _clamp(_number((board or {}).get("flow_percentile")) or (0.5 if board else 0.2))
    if turnover is None:
        activity_component = 0.5
    elif turnover > 25:
        activity_component = _clamp(1.0 - (turnover - 25) / 25)
    else:
        activity_component = _clamp(turnover / 6.0)
    if volume_ratio is not None and volume_ratio > 4.5:
        activity_component *= 0.65
    flow_ratio = (main_net / amount) if amount and main_net is not None else None
    flow_component = 0.5 if flow_ratio is None else _clamp(0.5 + flow_ratio * 8.0)
    score = 100 * (
        structure_component * 0.45 + liquidity_percentile * 0.15 + activity_component * 0.10
        + flow_component * 0.15 + board_component * 0.15
    )
    flags = []
    if amount is not None and amount < 120_000_000:
        flags.append("short_term_liquidity_below_120m")
    if turnover is not None and turnover < 1.5:
        flags.append("short_term_turnover_below_1_5pct")
    if flow_ratio is not None and flow_ratio < 0:
        flags.append("same_day_main_net_negative")
    return round(score, 2), {
        "structure": round(structure_component, 4), "liquidity": round(liquidity_percentile, 4),
        "activity": round(activity_component, 4), "individual_flow": round(flow_component, 4),
        "board_flow": round(board_component, 4),
    }, flags


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

    latest_by_symbol = {symbol: bars[-1] for symbol, bars in grouped.items()}
    liquidity_percentiles = _percentiles({
        symbol: _number(item.get("amount")) for symbol, item in latest_by_symbol.items()
    })

    proposals: dict[str, dict[str, Any]] = {}
    screen_observations: list[dict[str, Any]] = []
    strict_ready = provisional_ready = fresh_started = 0
    for symbol, bars in grouped.items():
        board = board_contexts.get(symbol)
        board_positive = bool(board and float(board.get("net_amount") or 0) > 0)
        risk_flags = [] if board else ["no_exact_board_mapping"]
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
                score, ranking_components, activity_flags = _ranking_score(
                    structure, latest_by_symbol[symbol], board, liquidity_percentiles[symbol],
                )
                ranked_structure = {**structure, "ranking_components": ranking_components,
                                    "market_activity": {key: latest_by_symbol[symbol].get(key) for key in (
                                        "amount", "turnover_rate", "volume_ratio", "main_net_amount", "pe", "pb",
                                    )}}
                proposals_for_symbol.append({
                    "symbol": symbol, "name": names.get(symbol), "candidate_type": "base_ready_30d",
                    "score": score, "structure": ranked_structure,
                    "board_context": board or {"exact_member_mapping": False},
                    "risk_flags": [*risk_flags, *activity_flags],
                })
        elif len(bars) >= 15:
            structure = forming_structure(bars)
            evaluated_structures["base_forming_15d"] = structure
            risk_flags = [*risk_flags, *list(structure.get("quality_flags") or [])]
            if structure.get("status") == "forming":
                provisional_ready += 1
                score, ranking_components, activity_flags = _ranking_score(
                    structure, latest_by_symbol[symbol], board, liquidity_percentiles[symbol],
                )
                ranked_structure = {**structure, "ranking_components": ranking_components,
                                    "market_activity": {key: latest_by_symbol[symbol].get(key) for key in (
                                        "amount", "turnover_rate", "volume_ratio", "main_net_amount", "pe", "pb",
                                    )}}
                proposals_for_symbol.append({
                    "symbol": symbol, "name": names.get(symbol), "candidate_type": "base_forming_15d",
                    "score": round(score * 0.94, 2), "structure": ranked_structure,
                    "board_context": board or {"exact_member_mapping": False},
                    "risk_flags": [*risk_flags, *activity_flags, "provisional_15_session_structure"],
                })
        if len(bars) >= 15:
            started = fresh_start_structure(bars)
            evaluated_structures["fresh_start_15d"] = started
            risk_flags = [*risk_flags, *list(started.get("quality_flags") or [])]
            if started.get("status") == "started":
                fresh_started += 1
                score, ranking_components, activity_flags = _ranking_score(
                    started, latest_by_symbol[symbol], board, liquidity_percentiles[symbol],
                )
                ranked_started = {**started, "ranking_components": ranking_components,
                                  "market_activity": {key: latest_by_symbol[symbol].get(key) for key in (
                                      "amount", "turnover_rate", "volume_ratio", "main_net_amount", "pe", "pb",
                                  )}}
                proposals_for_symbol.append({
                    "symbol": symbol, "name": names.get(symbol), "candidate_type": "fresh_start_15d",
                    "score": round(score * 0.90, 2), "structure": ranked_started,
                    "board_context": board or {"exact_member_mapping": False},
                    "risk_flags": [*risk_flags, *activity_flags, "provisional_15_session_structure"],
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
                reason_codes.append("no_exact_board_mapping")
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

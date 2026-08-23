"""Pure ranking for exact-board intraday research candidates."""

from __future__ import annotations

import re
from typing import Any, Callable


def select(
    items: list[dict[str, Any]], limit: int, *,
    rank: Callable[[list[float | None]], dict[int, float]],
    number: Callable[[Any], float | None],
) -> list[dict[str, Any]]:
    """Turn exact board-member evidence into bounded research candidates.

    This has no I/O and intentionally retains the established scoring and
    risk flags.  It is a selection aid only; it never promotes a candidate to
    an executable order.
    """
    by_taxonomy: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if int(item.get("mapped_members") or 0) > 0 and item.get("top_stocks"):
            by_taxonomy.setdefault(str(item.get("taxonomy_key")), []).append(item)

    board_scores: dict[tuple[str, str], float] = {}
    for taxonomy_key, boards in by_taxonomy.items():
        flow_ranks = rank([number(item.get("net_inflow")) for item in boards])
        change_ranks = rank([number(item.get("change_pct")) for item in boards])
        for index, board in enumerate(boards):
            flow = number(board.get("net_inflow"))
            board_scores[(taxonomy_key, str(board.get("sector_key")))] = round(
                35 * flow_ranks.get(index, 0.0) + 15 * change_ranks.get(index, 0.0) + (5 if (flow or 0) > 0 else 0), 2
            )

    proposals: dict[str, dict[str, Any]] = {}
    for taxonomy_key, boards in by_taxonomy.items():
        for board in boards:
            board_key = (taxonomy_key, str(board.get("sector_key")))
            for stock in board.get("top_stocks") or []:
                symbol = str(stock.get("symbol") or "")
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                    continue
                proposed = {
                    "symbol": symbol, "name": stock.get("name"), "taxonomy_key": taxonomy_key,
                    "sector_key": board_key[1], "sector_label": board.get("label"),
                    "board_score": board_scores.get(board_key, 0.0), "board_net_inflow": number(board.get("net_inflow")),
                    "board_change_pct": number(board.get("change_pct")), "main_net_inflow": number(stock.get("main_net_inflow")),
                    "volume_ratio": number(stock.get("volume_ratio")), "turnover_rate": number(stock.get("turnover_rate")),
                    "pct_change": number(stock.get("pct_change")), "turnover": number(stock.get("turnover")),
                }
                existing = proposals.get(symbol)
                if existing is None or proposed["board_score"] > existing["board_score"]:
                    proposals[symbol] = proposed

    candidates = list(proposals.values())
    flow_ranks = rank([candidate["main_net_inflow"] for candidate in candidates])
    for index, candidate in enumerate(candidates):
        volume_ratio, turnover_rate, pct_change = candidate["volume_ratio"], candidate["turnover_rate"], candidate["pct_change"]
        volume_score = min(max(volume_ratio or 0, 0), 6) / 6 * 15
        turnover_score = min(max(turnover_rate or 0, 0), 20) / 20 * 10
        price_score = min(max(pct_change or 0, 0), 8) / 8 * 5
        score = candidate["board_score"] + 20 * flow_ranks.get(index, 0.0) + volume_score + turnover_score + price_score
        flags = ["public_intraday_sources_only"]
        if (candidate["main_net_inflow"] or 0) <= 0:
            flags.append("nonpositive_main_net_inflow")
        if (candidate["board_net_inflow"] or 0) <= 0:
            flags.append("nonpositive_board_net_inflow")
        if (pct_change or 0) >= 8:
            flags.append("price_extension")
        if (turnover_rate or 0) >= 25:
            flags.append("very_high_turnover")
        hard_no_trade = {"nonpositive_main_net_inflow", "nonpositive_board_net_inflow"}.intersection(flags)
        if hard_no_trade:
            decision = "no_trade"
        elif score >= 70 and "price_extension" not in flags:
            decision = "research_candidate"
        else:
            decision = "watch"
        candidate.update({"score": round(max(0.0, min(score, 100.0)), 2), "decision": decision,
                          "confidence": round(min(0.7, 0.25 + score / 200), 3), "risk_flags": flags})
    candidates.sort(key=lambda item: (item["decision"] != "research_candidate", -item["score"], item["symbol"]))
    return candidates[:limit]


__all__ = ["select"]

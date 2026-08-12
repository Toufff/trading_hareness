"""Pure, evidence-first mining of individual stocks from live board snapshots.

The inputs are deliberately restricted to an exact board-member join and the
same Tencent cross-section already acquired for the five-minute board report.
This is a research-candidate screen, never an order or a replacement for the
separately validated intraday alert rules.
"""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _ranked(candidates: list[dict[str, Any]], direction: str, maximum: int) -> list[dict[str, Any]]:
    selected = sorted(
        (item for item in candidates if item["direction"] == direction),
        key=lambda item: (-float(item["score"]), item["symbol"], item["sector_key"]),
    )[:maximum]
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    return selected


def board_stock_mining_candidates(
    board_items: list[dict[str, Any]], *, max_per_direction: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return bounded stock candidates from exact, complete board membership.

    A candidate requires board flow to agree with individual indicative main
    flow and an existing intraday strategy-compatible activity proxy (volume
    ratio plus turnover/price behaviour).  The function intentionally skips
    partial or name-inferred memberships so a board label never fabricates a
    stock relationship.
    """
    maximum = max(1, min(int(max_per_direction), 50))
    exact_boards = 0
    skipped_partial = 0
    quoted_members = 0
    candidates: list[dict[str, Any]] = []

    for board in board_items:
        members = int(board.get("mapped_members") or 0)
        quoted = int(board.get("quoted_members") or 0)
        member_quotes = list(board.get("member_quotes") or [])
        if not members or quoted != members or len(member_quotes) != quoted:
            skipped_partial += 1
            continue
        board_flow = _number(board.get("net_inflow"))
        if board_flow is None or board_flow == 0:
            continue
        exact_boards += 1
        quoted_members += quoted
        flow_scale = max((abs(_number(row.get("main_net_inflow")) or 0.0) for row in member_quotes), default=1.0)
        flow_scale = max(flow_scale, 1.0)
        for stock in member_quotes:
            symbol = str(stock.get("symbol") or "").upper()
            main_flow = _number(stock.get("main_net_inflow"))
            volume_ratio = _number(stock.get("volume_ratio"))
            turnover_rate = _number(stock.get("turnover_rate"))
            pct_change = _number(stock.get("pct_change"))
            if not symbol or main_flow is None or volume_ratio is None or turnover_rate is None or pct_change is None:
                continue
            flow_strength = min(18.0, abs(main_flow) / flow_scale * 18.0)
            activity = min(16.0, max(0.0, volume_ratio - 1.0) * 10.0)
            turnover = min(10.0, max(0.0, turnover_rate) * 1.5)
            base = 48.0 + flow_strength + activity + turnover
            direction: str | None = None
            setup_key: str | None = None
            risk_flags: list[str] = []
            price_component = 0.0

            if board_flow > 0 and main_flow > 0 and volume_ratio >= 1.5:
                direction = "inflow"
                if pct_change >= 0:
                    setup_key = "board_inflow_leader"
                    price_component = min(10.0, pct_change * 2.0)
                else:
                    setup_key = "board_inflow_absorption"
                    price_component = min(4.0, abs(pct_change))
                    risk_flags.append("price_not_yet_reclaimed")
            elif board_flow < 0 and main_flow < 0 and volume_ratio >= 1.5 and pct_change <= 0:
                direction = "outflow"
                setup_key = "board_outflow_risk"
                price_component = min(10.0, abs(pct_change) * 2.0)

            if direction is None or setup_key is None:
                continue
            candidates.append({
                "direction": direction,
                "setup_key": setup_key,
                "symbol": symbol,
                "name": stock.get("name"),
                "taxonomy_key": str(board.get("taxonomy_key") or ""),
                "sector_key": str(board.get("sector_key") or ""),
                "label": str(board.get("label") or board.get("sector_key") or ""),
                "score": round(base + price_component, 2),
                "board_net_inflow": board_flow,
                "board_change_pct": _number(board.get("change_pct")),
                "main_net_inflow": main_flow,
                "volume_ratio": volume_ratio,
                "turnover_rate": turnover_rate,
                "pct_change": pct_change,
                "risk_flags": risk_flags,
                "evidence": {
                    "membership": "exact_complete",
                    "quote_snapshot": "tencent_same_board_report",
                    "strategy_components": ["board_flow", "main_net_inflow", "volume_ratio", "turnover_rate", "pct_change"],
                },
            })

    inflows = _ranked(candidates, "inflow", maximum)
    outflows = _ranked(candidates, "outflow", maximum)
    coverage = {
        "exact_complete_boards": exact_boards,
        "quoted_exact_members": quoted_members,
        "partial_or_unmapped_boards_skipped": skipped_partial,
        "candidate_policy": "exact member mapping plus same-board Tencent quote coverage only",
    }
    summary = {
        "inflow_candidates": len(inflows),
        "outflow_candidates": len(outflows),
        "returned": len(inflows) + len(outflows),
        "model_version": "board-flow-stock-mining-v1",
        "notice": "研究候选，不是买卖指令；仅在精确成员映射和同刻报价完整时生成。",
    }
    return inflows + outflows, coverage, summary

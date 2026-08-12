"""Research candidates linked to a live limit-up anchor by exact concepts."""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def limit_linkage_candidates(
    relations: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], *, maximum: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Screen non-limit-up peers sharing one or more exact THS concepts.

    A shared board is structural evidence, not causal proof.  The candidate
    must additionally have contemporaneous public flow, activity, and price
    evidence.  Limit-up anchors themselves and near-limit peers are excluded
    to avoid presenting a late board-chasing list as an entry signal.
    """
    bounded = max(1, min(int(maximum), 50))
    raw: list[dict[str, Any]] = []
    for relation in relations:
        symbol = str(relation.get("symbol") or "").upper()
        quote = quotes.get(symbol)
        if not quote:
            continue
        main_flow = _number(quote.get("main_net_inflow"))
        volume_ratio = _number(quote.get("volume_ratio"))
        turnover_rate = _number(quote.get("turnover_rate"))
        pct_change = _number(quote.get("pct_change"))
        shared = int(relation.get("shared_concepts") or 0)
        if (not symbol or shared < 1 or main_flow is None or main_flow <= 0 or volume_ratio is None
                or volume_ratio < 1.3 or turnover_rate is None or turnover_rate < 1.0
                or pct_change is None or not 1.0 <= pct_change < 7.0):
            continue
        raw.append({
            "symbol": symbol, "name": quote.get("name"), "score": 0.0,
            "shared_concepts": shared, "concept_labels": (relation.get("concept_labels") or [])[:5],
            # A large raw anchor list is a sign of common-concept overlap, not
            # stronger causality.  Keep the display evidence reviewable while
            # the relation count remains available in the stored summary.
            "leader_symbols": (relation.get("leader_symbols") or [])[:5],
            "leader_names": (relation.get("leader_names") or [])[:5],
            "pct_change": pct_change, "main_net_inflow": main_flow, "volume_ratio": volume_ratio,
            "turnover_rate": turnover_rate,
            "evidence": {"membership": "exact_ths_concept", "anchor": "eastmoney_limit_up_pool",
                         "quote_snapshot": "tencent_same_board_report",
                         "components": ["shared_concepts", "main_net_inflow", "volume_ratio", "turnover_rate", "pct_change"]},
            "risk_flags": ["leader_linkage_research_only", "requires_minute_confirmation"],
        })
    flow_scale = max((item["main_net_inflow"] for item in raw), default=1.0)
    for item in raw:
        item["score"] = round(
            38.0 + min(24.0, item["shared_concepts"] * 12.0)
            + min(16.0, item["main_net_inflow"] / max(flow_scale, 1.0) * 16.0)
            + min(12.0, max(0.0, item["volume_ratio"] - 1.0) * 8.0)
            + min(7.0, item["turnover_rate"] * 1.0)
            + min(8.0, item["pct_change"] * 1.2), 2,
        )
    selected = sorted(raw, key=lambda row: (-row["score"], row["symbol"]))[:bounded]
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    return selected, {
        "anchors": len({leader for item in relations for leader in item.get("leader_symbols") or []}),
        "exact_relation_rows": len(relations), "candidate_count": len(selected),
        "model_version": "limit-linkage-mining-v1",
        "notice": "涨停股的精确概念关联候选，不是追板或自动买入指令；候选必须通过后续分钟确认。",
    }

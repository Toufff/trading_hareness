"""Shared minimum research coverage for scans and pool decisions.

The lane representative is a coverage floor, never a cap on research.  A
downstream reviewer may (and normally should) review additional comparisons,
carry-over recommendations and user tracked stocks from the full intake.
"""
from __future__ import annotations

from copy import deepcopy


SECTIONS = (
    ("selected", "条件观察"),
    ("observation_list", "结构观察"),
    ("caution_list", "风险观察"),
)


def membership_index(scan: dict) -> dict[str, list[dict]]:
    """Return one strongest display membership per symbol and lane."""
    result: dict[str, list[dict]] = {}
    if scan.get("status") != "completed":
        return result
    for lane in scan.get("lanes", []):
        seen: set[str] = set()
        for section, state_label in SECTIONS:
            for index, row in enumerate(lane.get(section, []) or [], 1):
                symbol = row["symbol"]
                if symbol in seen:
                    continue
                seen.add(symbol)
                member = {
                    "lane": lane["key"],
                    "label": lane.get("label") or lane["key"],
                    "list": section,
                    "state_label": state_label,
                    "display_rank": index,
                    "total_matches": lane.get("total_matches", 0),
                    "reason": row.get("reason") or "该策略未提供文字理由",
                    "rank_score": row.get("rank_score"),
                    "metrics": deepcopy(row.get("metrics")),
                    "liquidity": deepcopy(row.get("liquidity")),
                    "ranking_components": deepcopy(row.get("ranking_components")),
                }
                if row.get("factor_overlay"):
                    member["factor_overlay"] = deepcopy(row["factor_overlay"])
                result.setdefault(symbol, []).append(member)
    return result


def lane_representatives(scan: dict) -> list[dict]:
    """Build the minimum same-run company-research coverage.

    Every non-empty lane contributes its first displayed row using the same
    order as the human report: executable selection, independent structural
    observation, then risk observation.  One stock shared by several lanes is
    researched once while retaining every lane-specific reason.
    """
    if scan.get("status") != "completed":
        return []
    memberships = membership_index(scan)
    targets: dict[str, dict] = {}
    for lane in scan.get("lanes", []):
        section = rows = None
        state_label = None
        for candidate_section, candidate_label in SECTIONS:
            candidate_rows = lane.get(candidate_section, []) or []
            if candidate_rows:
                section, rows, state_label = candidate_section, candidate_rows, candidate_label
                break
        if not rows:
            continue
        row = rows[0]
        peer = next((item for item in rows[1:] if item["symbol"] != row["symbol"]), None)
        comparison = (
            f"同组下一展示股票为{peer.get('name') or peer['symbol']}（{peer['symbol'].split('.')[0]}），"
            f"其筛选依据是：{peer.get('reason') or '未提供'}。"
            if peer else "本策略当前没有第二个不同股票可供同组比较。"
        )
        reason = (
            f"{lane.get('label') or lane['key']}的{state_label}首位：{row.get('reason') or '未提供'}。"
            f"全量匹配{lane.get('total_matches', 0)}只；{comparison}"
            "这是公司研究的最低覆盖对象，不是研究上限，也不是买入结论。"
        )
        if row.get("factor_overlay"):
            reason += row["factor_overlay"].get("explanation") or ""
        target = targets.setdefault(row["symbol"], {
            "symbol": row["symbol"], "name": row.get("name") or row["symbol"],
            "reasons": [], "representative_lanes": [],
        })
        target["reasons"].append(reason)
        target["representative_lanes"].append(lane["key"])
    return [
        {
            "symbol": symbol,
            "name": target["name"],
            "selection_reason": "\n".join(target["reasons"]),
            "memberships": memberships.get(symbol, []),
            "representative_lanes": target["representative_lanes"],
        }
        for symbol, target in targets.items()
    ]


__all__ = ["SECTIONS", "lane_representatives", "membership_index"]

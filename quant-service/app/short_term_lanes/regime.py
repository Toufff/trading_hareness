"""Market-regime router for short-term discovery lanes.

The router is deliberately separate from alpha rules.  It changes research
priority and can suppress fragile playbooks, but it never manufactures a
probability or upgrades a stock to an executable recommendation.
"""
from __future__ import annotations

from statistics import median


PRIORITIES: dict[str, dict[str, float]] = {
    "broad_risk_on": {
        "trend": 1.20, "expansion": 1.20, "contraction": 1.12,
        "rotation": 1.10, "pullback": 1.00, "event": 1.00,
        "accumulation": 0.90, "reclaim": 0.75, "relay": 0.90,
    },
    "mixed_rotation": {
        "rotation": 1.20, "contraction": 1.10, "event": 1.05,
        "pullback": 1.00, "accumulation": 1.00, "trend": 0.90,
        "expansion": 0.90, "reclaim": 0.90, "relay": 0.65,
    },
    "stabilizing": {
        "reclaim": 1.20, "pullback": 1.12, "contraction": 1.05,
        "accumulation": 1.00, "rotation": 0.95, "event": 0.95,
        "trend": 0.80, "expansion": 0.80, "relay": 0.50,
    },
    "risk_off": {
        "event": 0.80, "accumulation": 0.70, "reclaim": 0.55,
        "pullback": 0.55, "contraction": 0.50, "rotation": 0.45,
        "trend": 0.35, "expansion": 0.30, "relay": 0.00,
    },
}


def classify(features: dict[str, dict], sectors: dict[str, dict]) -> dict:
    if not features:
        return {
            "label": "unavailable", "research_budget": 0.0,
            "evidence": ["没有完整市场截面"], "strategy_priority": {},
            "probability_status": "not_calibrated",
        }
    breadth = sum(row["change_pct"] > 0 for row in features.values()) / len(features)
    median_r10 = median(row["return_10d"] for row in features.values())
    eligible_sectors = [row for row in sectors.values() if row.get("members", 0) >= 5]
    participation = (
        sum(row["up_fraction"] >= 0.5 and row["return10_median"] > median_r10 for row in eligible_sectors)
        / len(eligible_sectors) if eligible_sectors else 0.0
    )
    if breadth >= 0.58 and median_r10 >= 2.0 and participation >= 0.50:
        label, budget = "broad_risk_on", 1.0
    elif breadth <= 0.38 and median_r10 < 0:
        label, budget = "risk_off", 0.35
    elif breadth >= 0.50 and median_r10 < 0:
        label, budget = "stabilizing", 0.60
    else:
        label, budget = "mixed_rotation", 0.70
    return {
        "label": label,
        "research_budget": budget,
        "evidence": [
            f"全市场上涨占比{breadth:.1%}",
            f"10日收益中位数{median_r10:+.2f}%",
            f"强于市场且当日多数上涨的行业占比{participation:.1%}",
        ],
        "strategy_priority": PRIORITIES[label],
        "probability_status": "not_calibrated",
        "note": "市场状态只路由策略和风险预算，不等同次日上涨概率。",
    }


def route(lane: str, regime: dict) -> dict:
    weight = float(regime.get("strategy_priority", {}).get(lane, 1.0))
    state = "disabled" if weight <= 0 else "restricted" if weight < 0.6 else "preferred" if weight > 1.05 else "neutral"
    return {"state": state, "priority_weight": weight, "regime": regime.get("label", "unavailable")}


__all__ = ["classify", "route"]

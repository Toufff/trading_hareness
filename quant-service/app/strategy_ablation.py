"""Read-only projection for the joint strategy ablation ledger."""

from __future__ import annotations

from typing import Any


def ablation_scores(*, market_signal: float, analyst_signal: float | None,
                    has_analyst_evidence: bool, applied_weight: float,
                    risk_penalty: float = 0.0) -> dict[str, float]:
    """Return live, market-only, and bounded analyst-shadow scores.

    The shadow leg is deliberately a fixed 10% what-if.  It is evidence for
    offline comparison only; the live leg remains at the promotion-registry
    weight (currently zero).  Keeping this pure makes the no-live-effect
    guarantee easy to test without a database or market data.
    """
    market = float(market_signal) - float(risk_penalty)
    analyst = float(analyst_signal if analyst_signal is not None else market)
    live_weight = max(0.0, min(0.10, float(applied_weight))) if has_analyst_evidence else 0.0
    shadow_weight = 0.10 if has_analyst_evidence and analyst_signal is not None else 0.0

    def score(signal: float) -> float:
        return max(0.0, min(100.0, 50.0 + 50.0 * signal))

    return {
        "market_only_score": score(market),
        "analyst_shadow_score": score((1.0 - shadow_weight) * market + shadow_weight * analyst),
        "applied_score": score((1.0 - live_weight) * market + live_weight * analyst),
        "market_signal": market,
        "analyst_signal": analyst if analyst_signal is not None else None,
        "analyst_delta": score((1.0 - shadow_weight) * market + shadow_weight * analyst) - score(market),
        "shadow_weight": shadow_weight,
    }


def latest_strategy_ablation(database: Any, limit: int = 200) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    with database.transaction() as connection:
        run = connection.execute(
            """SELECT run_id,as_of_date,model_version,market_regime,status,created_at
                 FROM quant.recommendation_runs
                WHERE run_id IN (SELECT DISTINCT run_id FROM quant.strategy_ablation_observations)
                ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        rows = connection.execute(
            """SELECT run_id,symbol,market_only_score,analyst_shadow_score,applied_score,
                      market_signal,analyst_signal,analyst_delta,applied_analyst_weight,
                      analyst_execution_status,evidence,created_at
                 FROM quant.strategy_ablation_observations
                WHERE (%s::uuid IS NULL OR run_id=%s)
                ORDER BY created_at DESC,symbol LIMIT %s""",
            (run["run_id"] if run else None, run["run_id"] if run else None, bounded),
        ).fetchall()
    return {
        "run": dict(run) if run else None,
        "items": [dict(row) for row in rows],
        "market_only": True,
        "analyst_shadow": True,
        "live_effect": "none_until_promotion_registry_approval",
        "notice": "两套分数仅用于消融；当前分析师应用权重必须保持 0，不能改变实时规则或推荐阈值。",
    }


__all__ = ["ablation_scores", "latest_strategy_ablation"]

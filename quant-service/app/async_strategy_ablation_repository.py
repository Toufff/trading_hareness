"""Native async strategy-ablation read projection."""

from __future__ import annotations

from typing import Any


async def latest_strategy_ablation(async_database: Any, limit: int = 200) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT run_id,as_of_date,model_version,market_regime,status,created_at
                 FROM quant.recommendation_runs
                WHERE run_id IN (SELECT DISTINCT run_id FROM quant.strategy_ablation_observations)
                ORDER BY created_at DESC LIMIT 1"""
        )
        run = await result.fetchone()
        result = await connection.execute(
            """SELECT run_id,symbol,market_only_score,analyst_shadow_score,applied_score,
                      market_signal,analyst_signal,analyst_delta,applied_analyst_weight,
                      analyst_execution_status,evidence,created_at
                 FROM quant.strategy_ablation_observations
                WHERE (%s::uuid IS NULL OR run_id=%s)
                ORDER BY created_at DESC,symbol LIMIT %s""",
            (run["run_id"] if run else None, run["run_id"] if run else None, bounded),
        )
        rows = await result.fetchall()
    return {"run": run, "items": rows, "market_only": True, "analyst_shadow": True,
            "live_effect": "none_until_promotion_registry_approval",
            "notice": "两套分数仅用于消融；当前分析师应用权重必须保持 0，不能改变实时规则或推荐阈值。"}


__all__ = ["latest_strategy_ablation"]

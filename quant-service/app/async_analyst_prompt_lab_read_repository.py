"""Native-async read projection for the human-gated analyst Prompt Lab."""

from __future__ import annotations

from typing import Any


async def status(async_database: Any, limit: int = 100) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    async with async_database.transaction() as connection:
        candidates_result = await connection.execute(
            """SELECT c.candidate_id,c.analyst_id,c.variant_key,c.variant_version,c.status,c.created_at,
                      o.scope,o.subject_key,o.action,o.direction,o.strategy_available_at,
                      l.label,l.direction_correct,l.action_executable,l.reviewer,l.labelled_at
                 FROM quant.analyst_prompt_candidates c
                 JOIN quant.analyst_observations o USING(observation_id)
                 LEFT JOIN quant.analyst_prompt_gold_labels l USING(candidate_id)
                ORDER BY c.created_at DESC LIMIT %s""", (bounded,),
        )
        evaluations_result = await connection.execute(
            """SELECT evaluation_id,variant_key,variant_version,cutoff_at,status,sample_count,metrics,created_at
                 FROM quant.analyst_prompt_evaluation_runs ORDER BY cutoff_at DESC LIMIT %s""", (bounded,),
        )
        outcomes_result = await connection.execute(
            """SELECT methodology_version,horizon_minutes,status,count(*)::int count,
                      avg(directional_return) avg_directional_return
                 FROM quant.analyst_intraday_outcomes
                GROUP BY methodology_version,horizon_minutes,status
                ORDER BY methodology_version,horizon_minutes,status"""
        )
        candidates = [dict(row) for row in await candidates_result.fetchall()]
        evaluations = [dict(row) for row in await evaluations_result.fetchall()]
        outcomes = [dict(row) for row in await outcomes_result.fetchall()]
    return {"candidates": candidates, "evaluations": evaluations, "intraday_outcomes": outcomes,
            "live_effect": "none", "boundary": "human labels and out-of-sample promotion required"}


__all__ = ["status"]

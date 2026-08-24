"""Async read projection for the latest ten-day shadow run."""

from __future__ import annotations

from typing import Any


async def latest_ten_day_leader_rotation_pool(async_database: Any, *, limit: int = 90) -> dict[str, Any]:
    """Load the bounded latest cohort for a separate intraday evaluator."""
    bounded_limit = max(1, min(int(limit), 90))
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT run_id,run_key,as_of_date,strategy_available_at,model_version,status,
                      source_status,summary,created_at,updated_at
                 FROM quant.ten_day_leader_rotation_runs
                WHERE status IN ('completed','partial')
                ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        )
        run = await result.fetchone()
        if not run:
            return {"run": None, "candidates": []}
        result = await connection.execute(
            """SELECT board,board_rank,symbol,name,ten_day_return_pct,current_return_pct,
                      candidate_path,shadow_state,shadow_eligible,decision_eligible,evidence,
                      reason_codes,risk_flags,source_snapshot,discovered_at
                 FROM quant.ten_day_leader_rotation_candidates
                WHERE run_id=%s ORDER BY board,board_rank LIMIT %s""",
            (run["run_id"], bounded_limit),
        )
        candidates = await result.fetchall()
    return {"run": dict(run), "candidates": [dict(row) for row in candidates]}


async def latest_ten_day_leader_rotation(async_database: Any, *, limit: int = 90) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit), 90))
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT run_id,run_key,as_of_date,strategy_available_at,model_version,status,
                      source_status,summary,created_at,updated_at
                 FROM quant.ten_day_leader_rotation_runs
                ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        )
        run = await result.fetchone()
        if not run:
            return {
                "run": None, "candidates": [], "intraday": {"pool_run": None, "latest_batch": None}, "scope": "research_only_no_orders",
                "notice": "尚未运行十日排行榜龙头协同影子研究。",
            }
        result = await connection.execute(
            """SELECT c.board,c.board_rank,c.symbol,c.name,c.ten_day_return_pct,c.current_return_pct,
                      c.candidate_path,c.shadow_state,c.shadow_eligible,c.decision_eligible,c.evidence,
                      c.reason_codes,c.risk_flags,c.source_snapshot,c.discovered_at,
                      o.observed_at AS intraday_observed_at,o.quote_source AS intraday_quote_source,
                      o.shadow_state AS intraday_shadow_state,o.shadow_eligible AS intraday_shadow_eligible,
                      o.evidence AS intraday_evidence,o.reason_codes AS intraday_reason_codes,
                      o.risk_flags AS intraday_risk_flags
                 FROM quant.ten_day_leader_rotation_candidates c
                 LEFT JOIN LATERAL (
                    SELECT observed_at,quote_source,shadow_state,shadow_eligible,evidence,reason_codes,risk_flags
                      FROM quant.ten_day_leader_rotation_intraday_observations
                     WHERE run_id=c.run_id AND symbol=c.symbol
                     ORDER BY observed_at DESC LIMIT 1
                 ) o ON true
                WHERE c.run_id=%s ORDER BY c.board,c.board_rank LIMIT %s""",
            (run["run_id"], bounded_limit),
        )
        candidates = await result.fetchall()
        result = await connection.execute(
            """SELECT run_id,run_key,as_of_date,strategy_available_at,model_version,status,
                      source_status,summary,created_at,updated_at
                 FROM quant.ten_day_leader_rotation_runs
                WHERE status IN ('completed','partial')
                ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        )
        intraday_pool_run = await result.fetchone()
        latest_intraday_batch = None
        if intraday_pool_run:
            result = await connection.execute(
                """WITH latest_batch AS (
                       SELECT scan_id,observed_at
                         FROM quant.ten_day_leader_rotation_intraday_observations
                        WHERE run_id=%s
                        GROUP BY scan_id,observed_at
                        ORDER BY observed_at DESC LIMIT 1
                   )
                   SELECT b.scan_id,b.observed_at,
                          count(*) AS observed_count,
                          count(*) FILTER (WHERE o.shadow_eligible) AS shadow_eligible_count,
                          count(*) FILTER (WHERE o.decision_eligible) AS decision_eligible_count,
                          array_agg(DISTINCT o.quote_source ORDER BY o.quote_source) AS quote_sources
                     FROM latest_batch b
                     JOIN quant.ten_day_leader_rotation_intraday_observations o
                       ON o.scan_id=b.scan_id AND o.observed_at=b.observed_at
                    WHERE o.run_id=%s
                    GROUP BY b.scan_id,b.observed_at""",
                (intraday_pool_run["run_id"], intraday_pool_run["run_id"]),
            )
            latest_intraday_batch = await result.fetchone()
    return {
        "run": dict(run), "candidates": [dict(row) for row in candidates],
        "intraday": {
            "pool_run": dict(intraday_pool_run) if intraday_pool_run else None,
            "latest_batch": dict(latest_intraday_batch) if latest_intraday_batch else None,
        },
        "scope": "research_only_no_orders",
        "notice": "候选由盘后排名生成；盘中影子观测仅使用已接收的周期、精确同板块和分钟证据，永不自动下单。",
    }


__all__ = ["latest_ten_day_leader_rotation", "latest_ten_day_leader_rotation_pool"]

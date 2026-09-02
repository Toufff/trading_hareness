"""Native async read projections for the strategy dashboard."""

from __future__ import annotations

from typing import Any

from .repo_common import bounded_limit

#: A recommendation run's rows are bounded by its own upstream universe size
#: (single digits of thousands at most), but the read was previously
#: unbounded ``SELECT *``; cap it defensively and surface truncation instead
#: of silently growing the response with the universe.
_MAX_RECOMMENDATIONS = 2000


async def latest_strategy_decision(async_database: Any, model_version: str) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        run = await connection.execute(
            "SELECT run_id,as_of_date,model_version,market_regime,source_status,created_at "
            "FROM quant.recommendation_runs WHERE model_version=%s ORDER BY created_at DESC LIMIT 1",
            (model_version,),
        )
        run = await run.fetchone()
        if not run:
            return {"run": None, "recommendations": [], "truncated": False}
        limit = bounded_limit(_MAX_RECOMMENDATIONS, _MAX_RECOMMENDATIONS)
        rows = await connection.execute(
            "SELECT run_id,rank,symbol,decision,score,score_breakdown,explanation,risk_flags "
            "FROM quant.recommendations WHERE run_id=%s ORDER BY rank LIMIT %s",
            (run["run_id"], limit + 1),
        )
        rows = await rows.fetchall()
    truncated = len(rows) > limit
    return {"run": run, "recommendations": rows[:limit], "truncated": truncated}


async def latest_strategy_review(async_database: Any, session: str | None) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        where, params = ("WHERE session=%s", (session,)) if session else ("", ())
        result = await connection.execute(
            f"SELECT review_id,review_key,exchange_date,session,observed_at,market_state,data_boundary,report,created_at "
            f"FROM quant.strategy_review_runs {where} ORDER BY observed_at DESC LIMIT 1", params,
        )
        row = await result.fetchone()
    return {"review": row, "notice": "复盘是点时证据与情景准备，不是自动委托。"}


async def latest_post_close_strategy(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        attempt_result = await connection.execute(
            """SELECT run_id,run_key,as_of_date,model_version,status,source_status,summary,created_at,updated_at
                 FROM quant.post_close_strategy_runs ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        )
        latest_attempt = await attempt_result.fetchone()
        completed_result = await connection.execute(
            """SELECT run_id,run_key,as_of_date,model_version,status,source_status,summary,created_at,updated_at
                 FROM quant.post_close_strategy_runs WHERE status IN ('completed','partial')
                 ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        )
        latest_completed = await completed_result.fetchone()
        if not latest_completed:
            return {
                "run": latest_attempt,
                "latest_attempt": latest_attempt,
                "latest_completed": None,
                "candidate_run": None,
                "candidates": [],
                "notice": "尚未得到可用的盘后蓄势/首动研究。",
            }
        candidates_result = await connection.execute(
            """SELECT c.rank,c.symbol,i.name,c.candidate_type,c.score,c.structure,c.board_context,c.risk_flags,
                          c.discovered_at,c.expires_at,c.reason_codes,c.source_snapshot
                 FROM quant.post_close_strategy_candidates c LEFT JOIN quant.instruments i ON i.symbol=c.symbol
                WHERE c.run_id=%s ORDER BY c.rank""", (latest_completed["run_id"],),
        )
        rows = await candidates_result.fetchall()
    return {
        "run": latest_attempt,
        "latest_attempt": latest_attempt,
        "latest_completed": latest_completed,
        "candidate_run": latest_completed,
        "candidates": rows,
        "notice": "候选用于次日人工观察；未自动加入盘中观察池，也不会自动下单。",
    }


__all__ = ["latest_strategy_decision", "latest_strategy_review", "latest_post_close_strategy"]

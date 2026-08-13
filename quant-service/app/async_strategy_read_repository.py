"""Native async read projections for the strategy dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any


async def latest_strategy_decision(async_database: Any, model_version: str) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        run = await connection.execute(
            "SELECT * FROM quant.recommendation_runs WHERE model_version=%s ORDER BY created_at DESC LIMIT 1",
            (model_version,),
        )
        run = await run.fetchone()
        if not run:
            return {"run": None, "recommendations": []}
        rows = await connection.execute(
            "SELECT * FROM quant.recommendations WHERE run_id=%s ORDER BY rank", (run["run_id"],)
        )
        rows = await rows.fetchall()
    return {"run": run, "recommendations": rows}


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
        run_result = await connection.execute(
            """SELECT run_id,run_key,as_of_date,model_version,status,source_status,summary,created_at,updated_at
                 FROM quant.post_close_strategy_runs WHERE status IN ('completed','partial')
                 ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        )
        run = await run_result.fetchone()
        if not run:
            return {"run": None, "latest_attempt": latest_attempt, "candidates": [],
                    "notice": "尚未得到可用的盘后蓄势/首动研究。"}
        candidates_result = await connection.execute(
            """SELECT c.rank,c.symbol,i.name,c.candidate_type,c.score,c.structure,c.board_context,c.risk_flags,
                          c.discovered_at,c.expires_at,c.reason_codes,c.source_snapshot
                 FROM quant.post_close_strategy_candidates c LEFT JOIN quant.instruments i ON i.symbol=c.symbol
                WHERE c.run_id=%s ORDER BY c.rank""", (run["run_id"],),
        )
        rows = await candidates_result.fetchall()
    return {"run": run, "latest_attempt": latest_attempt, "candidates": rows,
            "notice": "候选用于次日人工观察；未自动加入盘中观察池，也不会自动下单。"}


__all__ = ["latest_strategy_decision", "latest_strategy_review", "latest_post_close_strategy"]

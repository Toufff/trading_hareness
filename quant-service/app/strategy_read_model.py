"""Read-only projections of already materialized strategy evidence."""

from __future__ import annotations

from typing import Any


def latest_strategy_decision(database: Any, model_version: str) -> dict[str, Any]:
    with database.transaction() as connection:
        run = connection.execute(
            "SELECT * FROM quant.recommendation_runs WHERE model_version=%s ORDER BY created_at DESC LIMIT 1",
            (model_version,),
        ).fetchone()
        if not run:
            return {"run": None, "recommendations": []}
        rows = connection.execute("SELECT * FROM quant.recommendations WHERE run_id=%s ORDER BY rank", (run["run_id"],)).fetchall()
    return {"run": run, "recommendations": rows}


def latest_strategy_review(database: Any, session: str | None) -> dict[str, Any]:
    with database.transaction() as connection:
        where, params = ("WHERE session=%s", (session,)) if session else ("", ())
        row = connection.execute(
            f"SELECT review_id,review_key,exchange_date,session,observed_at,market_state,data_boundary,report,created_at "
            f"FROM quant.strategy_review_runs {where} ORDER BY observed_at DESC LIMIT 1", params,
        ).fetchone()
    return {"review": row, "notice": "复盘是点时证据与情景准备，不是自动委托。"}


def latest_post_close_strategy(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        latest_attempt = connection.execute(
            """SELECT run_id,run_key,as_of_date,model_version,status,source_status,summary,created_at,updated_at
                 FROM quant.post_close_strategy_runs ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        ).fetchone()
        run = connection.execute(
            """SELECT run_id,run_key,as_of_date,model_version,status,source_status,summary,created_at,updated_at
                 FROM quant.post_close_strategy_runs WHERE status IN ('completed','partial')
                 ORDER BY as_of_date DESC,updated_at DESC LIMIT 1"""
        ).fetchone()
        if not run:
            return {"run": None, "latest_attempt": latest_attempt, "candidates": [], "notice": "尚未得到可用的盘后蓄势/首动研究。"}
        rows = connection.execute(
            """SELECT c.rank,c.symbol,i.name,c.candidate_type,c.score,c.structure,c.board_context,c.risk_flags
                 FROM quant.post_close_strategy_candidates c LEFT JOIN quant.instruments i ON i.symbol=c.symbol
                WHERE c.run_id=%s ORDER BY c.rank""", (run["run_id"],),
        ).fetchall()
    return {"run": run, "latest_attempt": latest_attempt, "candidates": rows,
            "notice": "候选用于次日人工观察；未自动加入盘中观察池，也不会自动下单。"}

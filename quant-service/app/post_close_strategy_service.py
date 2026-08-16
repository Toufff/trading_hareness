"""Persisted-only post-close candidate service.

The screening rules stay in ``post_close_candidate_screen``; this module owns
the database boundary and the same-date run contract.  It deliberately has no
provider dependency, so retrying a delayed evening pipeline can never start a
new historical fetch or turn yesterday's complete cross-section into today's
success.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


def candidates(
    database: Any,
    as_of_date: date,
    limit: int,
    minimum_full_market_symbols: int,
    *,
    board_context: Callable[[date], dict[str, dict[str, Any]]],
    screen: Callable[..., dict[str, Any]],
    daily_base_structure: Callable[[list[dict[str, Any]]], dict[str, Any]],
    forming_structure: Callable[[list[dict[str, Any]]], dict[str, Any]],
    fresh_start_structure: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    """Screen only already-persisted bars and exact point-in-time mappings."""
    with database.transaction() as connection:
        coverage = connection.execute(
            """SELECT count(DISTINCT symbol)::int AS symbols
                 FROM quant.canonical_bars_daily WHERE trading_date=%s AND symbol<>'000300.SH'""",
            (as_of_date,),
        ).fetchone()
        rows = connection.execute(
            """WITH ranked AS (
                   SELECT b.symbol,b.trading_date,b.high,b.low,b.close,b.volume,b.adj_factor,i.name,
                          row_number() OVER (PARTITION BY b.symbol ORDER BY b.trading_date DESC) AS rn
                     FROM quant.canonical_bars_daily b LEFT JOIN quant.instruments i ON i.symbol=b.symbol
                    WHERE b.symbol<>'000300.SH' AND b.trading_date<=%s AND b.trading_date>=%s
                      AND b.quality_status IN ('fresh','partial')
                 ) SELECT symbol,trading_date,high,low,close,volume,adj_factor,name
                    FROM ranked WHERE rn<=30 ORDER BY symbol,trading_date""",
            (as_of_date, as_of_date - timedelta(days=70)),
        ).fetchall()
    return screen(
        as_of_date, limit, minimum_full_market_symbols, int(coverage["symbols"] or 0),
        [dict(row) for row in rows], board_context(as_of_date),
        daily_base_structure=daily_base_structure, forming_structure=forming_structure,
        fresh_start_structure=fresh_start_structure,
    )


def run(
    database: Any,
    request: Any,
    *,
    model_version: str,
    candidate_loader: Callable[[date, int, int], dict[str, Any]],
    json_safe: Callable[[Any], Any],
) -> dict[str, Any]:
    """Persist an exact-date screen, including an explicit blocked attempt."""
    with database.transaction() as connection:
        latest = connection.execute(
            """SELECT trading_date FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH'
                 GROUP BY trading_date HAVING count(DISTINCT symbol)>=%s ORDER BY trading_date DESC LIMIT 1""",
            (request.minimum_full_market_symbols,),
        ).fetchone()
    # A caller-supplied date always remains the target date.  The convenience
    # fallback is for manual undated calls only.
    as_of_date = request.as_of_date or (latest["trading_date"] if latest else None)
    if as_of_date is None:
        return {
            "status": "blocked", "as_of_date": None, "candidates": [],
            "reason": "no full-market daily bar set is stored",
        }
    result = candidate_loader(as_of_date, request.limit, request.minimum_full_market_symbols)
    run_key = hashlib.sha256(f"{model_version}:{as_of_date}".encode()).hexdigest()
    with database.transaction() as connection:
        run_row = connection.execute(
            """INSERT INTO quant.post_close_strategy_runs(run_key,as_of_date,model_version,status,source_status,summary)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(run_key) DO UPDATE SET status=EXCLUDED.status,source_status=EXCLUDED.source_status,
                   summary=EXCLUDED.summary,updated_at=now()
               RETURNING run_id""",
            (run_key, as_of_date, model_version, result["status"],
             Json(json_safe(result.get("source_status", {}))),
             Json(json_safe({**result.get("summary", {}), "reason": result.get("reason")}))),
        ).fetchone()
        connection.execute("DELETE FROM quant.post_close_strategy_candidates WHERE run_id=%s", (run_row["run_id"],))
        for rank, candidate in enumerate(result.get("candidates", []), start=1):
            source_snapshot = {
                "as_of_date": str(as_of_date), "model_version": model_version,
                "daily_bars": result.get("source_status", {}).get("daily_bars"),
                "daily_symbols": result.get("source_status", {}).get("daily_symbols"),
                "exact_board_context_symbols": result.get("source_status", {}).get("exact_board_context_symbols"),
            }
            connection.execute(
                """INSERT INTO quant.post_close_strategy_candidates(
                           run_id,rank,symbol,candidate_type,score,structure,board_context,risk_flags,
                           discovered_at,expires_at,reason_codes,source_snapshot)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s)""",
                (run_row["run_id"], rank, candidate["symbol"], candidate["candidate_type"], candidate["score"],
                 Json(json_safe(candidate["structure"])), Json(json_safe(candidate["board_context"])),
                 Json(candidate["risk_flags"]), as_of_date + timedelta(days=1),
                 Json(candidate["risk_flags"]), Json(json_safe(source_snapshot))),
            )
    return {**result, "run_id": str(run_row["run_id"]), "run_key": run_key, "model_version": model_version}


def retry_window(value: datetime) -> bool:
    """Allow retries only in the same Shanghai post-close evening window."""
    local = value.astimezone(ZoneInfo("Asia/Shanghai"))
    return time(18, 55) <= local.time() < time(20, 30)


def completed_for_date(database: Any, as_of_date: date, *, model_version: str) -> bool:
    """Return completion only for the requested date and model version."""
    run_key = hashlib.sha256(f"{model_version}:{as_of_date}".encode()).hexdigest()
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT status FROM quant.post_close_strategy_runs WHERE run_key=%s", (run_key,),
        ).fetchone()
    return bool(row and row["status"] in {"completed", "partial"})


__all__ = ["candidates", "completed_for_date", "retry_window", "run"]

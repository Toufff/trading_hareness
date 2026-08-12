"""Persistence for bounded board-flow stock-mining evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Json


def persist_board_stock_mining_run(
    connection: Any,
    *,
    board_report_id: Any,
    observed_at: datetime,
    candidates: list[dict[str, Any]],
    coverage: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    row = connection.execute(
        """INSERT INTO quant.intraday_board_stock_mining_runs(
               board_report_id,observed_at,status,coverage,summary
           ) VALUES(%s,%s,'completed',%s,%s)
           ON CONFLICT(board_report_id) DO UPDATE SET
               observed_at=EXCLUDED.observed_at,status=EXCLUDED.status,
               coverage=EXCLUDED.coverage,summary=EXCLUDED.summary
           RETURNING mining_run_id""",
        (board_report_id, observed_at, Json(coverage), Json(summary)),
    ).fetchone()
    mining_run_id = row["mining_run_id"]
    connection.execute("DELETE FROM quant.intraday_board_stock_mining_candidates WHERE mining_run_id=%s", (mining_run_id,))
    for candidate in candidates:
        connection.execute(
            """INSERT INTO quant.intraday_board_stock_mining_candidates(
                   mining_run_id,rank,direction,setup_key,symbol,name,taxonomy_key,sector_key,label,score,
                   board_net_inflow,board_change_pct,main_net_inflow,volume_ratio,turnover_rate,pct_change,
                   evidence,risk_flags
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (mining_run_id, candidate["rank"], candidate["direction"], candidate["setup_key"], candidate["symbol"],
             candidate.get("name"), candidate["taxonomy_key"], candidate["sector_key"], candidate["label"], candidate["score"],
             candidate.get("board_net_inflow"), candidate.get("board_change_pct"), candidate.get("main_net_inflow"),
             candidate.get("volume_ratio"), candidate.get("turnover_rate"), candidate.get("pct_change"),
             Json(candidate.get("evidence") or {}), Json(candidate.get("risk_flags") or [])),
        )
    return str(mining_run_id)

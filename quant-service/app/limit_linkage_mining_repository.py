"""Persistence for bounded live limit-up linkage candidates."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Json


def persist_limit_linkage_mining_run(connection: Any, *, observed_at: datetime, trade_date: Any,
                                     candidates: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    row = connection.execute(
        """INSERT INTO quant.intraday_limit_linkage_mining_runs(observed_at,trade_date,status,summary)
           VALUES(%s,%s,'completed',%s) RETURNING linkage_run_id""",
        (observed_at, trade_date, Json(summary)),
    ).fetchone()
    run_id = row["linkage_run_id"]
    for candidate in candidates:
        connection.execute(
            """INSERT INTO quant.intraday_limit_linkage_candidates(
                   linkage_run_id,rank,symbol,name,score,shared_concepts,concept_labels,leader_symbols,leader_names,
                   pct_change,main_net_inflow,volume_ratio,turnover_rate,evidence,risk_flags
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, candidate["rank"], candidate["symbol"], candidate.get("name"), candidate["score"],
             candidate["shared_concepts"], Json(candidate["concept_labels"]), Json(candidate["leader_symbols"]),
             Json(candidate["leader_names"]), candidate["pct_change"], candidate["main_net_inflow"],
             candidate["volume_ratio"], candidate["turnover_rate"], Json(candidate["evidence"]), Json(candidate["risk_flags"])),
        )
    return str(run_id)

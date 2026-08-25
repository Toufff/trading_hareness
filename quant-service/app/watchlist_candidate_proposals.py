"""Daily cross-strategy watchlist proposals, deliberately kept out of the live watchlist.

The only real bridge between selection and timing in this codebase is manual:
a human PUTs a symbol into ``intraday_watchlists`` via the API. That table
also has a hard, previously-verified 40-symbol capacity bound tied to the
Tencent batched-quote request size the live intraday scan can make in one
call - a human-curated watchlist already uses most of it (37/40 at the time
this module was written). Auto-writing candidates into that table would risk
pushing the live scan over its capacity and starving real alerting.

This module instead materializes a separate, read-only daily proposal list
from the unified strategy_daily_candidates ledger: liquidity-eligible
candidates, ranked by percentile *within* their own strategy_key (their raw
scores are not on the same scale - see strategy_daily_candidate_ledger.py's
score_scale field) and deduplicated to the single best-ranked strategy per
symbol. It never touches intraday_watchlists; a human still decides what
actually goes live.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Json

DEFAULT_TOP_K = 15


def materialize_watchlist_proposals(connection: Any, as_of_date: date, *, top_k: int = DEFAULT_TOP_K) -> int:
    connection.execute("DELETE FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s", (as_of_date,))
    rows = connection.execute(
        """WITH scored AS (
                SELECT strategy_key,symbol,raw_score,score_scale,rank,evidence,
                  percent_rank() OVER (PARTITION BY strategy_key ORDER BY raw_score) strategy_percentile
                FROM quant.strategy_daily_candidates
               WHERE as_of_date=%s AND liquidity_eligible AND raw_score IS NOT NULL
             ), best_per_symbol AS (
                SELECT DISTINCT ON (symbol) symbol,strategy_key,raw_score,score_scale,rank,strategy_percentile,evidence
                  FROM scored ORDER BY symbol,strategy_percentile DESC
             )
             SELECT * FROM best_per_symbol ORDER BY strategy_percentile DESC LIMIT %s""",
        (as_of_date, top_k),
    ).fetchall()
    for proposal_rank, row in enumerate(rows, start=1):
        connection.execute(
            """INSERT INTO quant.strategy_watchlist_proposals(
                    as_of_date,symbol,proposal_rank,strategy_key,raw_score,score_scale,strategy_percentile,evidence)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (as_of_date, row["symbol"], proposal_rank, row["strategy_key"], row["raw_score"], row["score_scale"],
             row["strategy_percentile"], Json(row["evidence"] or {})),
        )
    return len(rows)


def latest_watchlist_proposals(connection: Any) -> dict[str, Any]:
    as_of = connection.execute("SELECT max(as_of_date) d FROM quant.strategy_watchlist_proposals").fetchone()["d"]
    if as_of is None:
        return {"as_of_date": None, "proposals": []}
    rows = connection.execute(
        """SELECT symbol,proposal_rank,strategy_key,raw_score,score_scale,strategy_percentile,evidence
             FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s ORDER BY proposal_rank""",
        (as_of,),
    ).fetchall()
    return {"as_of_date": str(as_of), "proposals": [dict(row) for row in rows],
            "notice": "research proposal only; never written into quant.intraday_watchlists"}


def sync_latest_watchlist_proposals(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        return latest_watchlist_proposals(connection)


__all__ = ["DEFAULT_TOP_K", "latest_watchlist_proposals", "materialize_watchlist_proposals", "sync_latest_watchlist_proposals"]

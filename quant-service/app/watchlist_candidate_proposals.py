"""Daily cross-strategy watchlist proposals, deliberately kept out of the live watchlist.

The only real bridge between selection and timing in this codebase is manual:
a human PUTs a symbol into ``intraday_watchlists`` via the API. That table
also has a hard, previously-verified 40-symbol capacity bound tied to the
Tencent batched-quote request size the live intraday scan can make in one
call - a human-curated watchlist already uses most of it (37/40 at the time
this module was written). Auto-writing candidates into that table would risk
pushing the live scan over its capacity and starving real alerting.

This module instead materializes a separate, read-only daily proposal list
from two independent producers, tagged by ``proposal_source``:

``strategy_ledger``      liquidity-eligible candidates from the unified
                         strategy_daily_candidates ledger, ranked by percentile
                         *within* their own strategy_key (their raw scores are
                         not on the same scale - see
                         strategy_daily_candidate_ledger.py's score_scale
                         field) and deduplicated to the single best-ranked
                         strategy per symbol.
``disclosure_day_watch`` names whose report is registered for the next session
                         and that carry no prior guidance - a scheduled
                         catalyst rather than a scored candidate, so these rows
                         have no raw_score or percentile at all.  See
                         disclosure_day_watch.py for the event study.

A symbol produced by both keeps its scored ledger row, with the disclosure
evidence merged in, because a score plus a catalyst is strictly more
informative than the catalyst alone.

It never touches intraday_watchlists; a human still decides what actually goes
live.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Json

from .disclosure_day_watch import (
    DEFAULT_TOP_K as DISCLOSURE_TOP_K,
    PROPOSAL_SOURCE as DISCLOSURE_SOURCE,
    STRATEGY_KEY as DISCLOSURE_STRATEGY_KEY,
    next_trading_session,
    rank_disclosure_watch,
    scheduled_disclosures,
)
from .liquidity_screen import liquidity_eligibility, median_daily_amount_by_symbol

DEFAULT_TOP_K = 15
LEDGER_SOURCE = "strategy_ledger"


def _liquidity_for(connection: Any, symbols: list[str], as_of_date: date) -> tuple[dict[str, dict[str, Any]], dict[str, float | None]]:
    """Screen an arbitrary symbol list with the same floors the ledger uses."""
    if not symbols:
        return {}, {}
    traded_value = median_daily_amount_by_symbol(connection, symbols, as_of_date)
    instruments = {
        str(row["symbol"]): dict(row) for row in connection.execute(
            "SELECT symbol,is_st,list_date FROM quant.instruments WHERE symbol=ANY(%s)", (symbols,),
        ).fetchall()
    }
    bars = {
        str(row["symbol"]): dict(row) for row in connection.execute(
            """SELECT DISTINCT ON (symbol) symbol,close,is_suspended FROM quant.canonical_bars_daily
                 WHERE symbol=ANY(%s) AND trading_date<=%s ORDER BY symbol,trading_date DESC""",
            (symbols, as_of_date),
        ).fetchall()
    }
    screen: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        instrument, bar = instruments.get(symbol) or {}, bars.get(symbol) or {}
        close = bar.get("close")
        eligible, flags = liquidity_eligibility(
            median_daily_amount=traded_value.get(symbol),
            latest_price=float(close) if close is not None else None,
            list_date=instrument.get("list_date"), as_of_date=as_of_date,
            is_st=bool(instrument.get("is_st")), is_suspended=bool(bar.get("is_suspended")),
        )
        screen[symbol] = {"eligible": eligible, "flags": flags}
    return screen, traded_value


def materialize_disclosure_day_watch(connection: Any, as_of_date: date, *,
                                     top_k: int = DISCLOSURE_TOP_K) -> dict[str, Any]:
    """Rank next-session scheduled disclosers without a prior guidance release."""
    session = next_trading_session(connection, as_of_date)
    if session is None:
        return {"status": "blocked", "reason": "no future open session in quant.market_trade_calendar",
                "selected": []}
    candidates = scheduled_disclosures(connection, session)
    if not candidates:
        return {"status": "empty", "session": str(session), "considered": 0, "selected": []}
    screen, traded_value = _liquidity_for(connection, [str(row["symbol"]) for row in candidates], as_of_date)
    ranked = rank_disclosure_watch(candidates, screen, traded_value, top_k=top_k)
    return {"status": "completed", "session": str(session), **ranked}


def materialize_watchlist_proposals(connection: Any, as_of_date: date, *, top_k: int = DEFAULT_TOP_K,
                                    disclosure_top_k: int = DISCLOSURE_TOP_K) -> dict[str, Any]:
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
    disclosure = materialize_disclosure_day_watch(connection, as_of_date, top_k=disclosure_top_k)
    disclosure_by_symbol = {item["symbol"]: item for item in disclosure["selected"]}
    proposal_rank = 0
    # The scored ledger is written first so it wins the (as_of_date, symbol)
    # key; a symbol that is also a scheduled discloser keeps its score and
    # gains the catalyst in its evidence rather than losing one of the two.
    for row in rows:
        proposal_rank += 1
        evidence = dict(row["evidence"] or {})
        overlap = disclosure_by_symbol.pop(str(row["symbol"]), None)
        if overlap is not None:
            evidence["disclosure_day_watch"] = overlap["evidence"]
        connection.execute(
            """INSERT INTO quant.strategy_watchlist_proposals(
                    as_of_date,symbol,proposal_rank,strategy_key,raw_score,score_scale,
                    strategy_percentile,evidence,proposal_source)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (as_of_date, row["symbol"], proposal_rank, row["strategy_key"], row["raw_score"], row["score_scale"],
             row["strategy_percentile"], Json(evidence), LEDGER_SOURCE),
        )
    for symbol, item in disclosure_by_symbol.items():
        proposal_rank += 1
        connection.execute(
            """INSERT INTO quant.strategy_watchlist_proposals(
                    as_of_date,symbol,proposal_rank,strategy_key,raw_score,score_scale,
                    strategy_percentile,evidence,proposal_source)
               VALUES(%s,%s,%s,%s,NULL,'unscored_event_watch',NULL,%s,%s)""",
            (as_of_date, symbol, proposal_rank, DISCLOSURE_STRATEGY_KEY, Json(item["evidence"]), DISCLOSURE_SOURCE),
        )
    return {"stored": proposal_rank, "strategy_ledger": len(rows),
            "disclosure_day_watch": len(disclosure_by_symbol), "disclosure": disclosure}


def latest_watchlist_proposals(connection: Any) -> dict[str, Any]:
    as_of = connection.execute("SELECT max(as_of_date) d FROM quant.strategy_watchlist_proposals").fetchone()["d"]
    if as_of is None:
        return {"as_of_date": None, "proposals": []}
    rows = connection.execute(
        """SELECT symbol,proposal_rank,strategy_key,raw_score,score_scale,strategy_percentile,evidence,proposal_source
             FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s ORDER BY proposal_rank""",
        (as_of,),
    ).fetchall()
    return {"as_of_date": str(as_of), "proposals": [dict(row) for row in rows],
            "notice": "research proposal only; never written into quant.intraday_watchlists"}


def sync_latest_watchlist_proposals(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        return latest_watchlist_proposals(connection)


__all__ = [
    "DEFAULT_TOP_K", "DISCLOSURE_TOP_K", "LEDGER_SOURCE", "latest_watchlist_proposals",
    "materialize_disclosure_day_watch", "materialize_watchlist_proposals", "sync_latest_watchlist_proposals",
]

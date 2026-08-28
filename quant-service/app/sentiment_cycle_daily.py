"""Materialize the daily short-term sentiment reading from closed daily bars.

``sentiment_cycle`` holds the arithmetic as pure functions; this reads the
sessions they need and writes one row per trading day, the same shape
``market_regime_daily`` uses for the index classifier.

The ladder height needs several prior sessions, so this loads a short window
ending on the requested date rather than that date alone.  Instruments without
a listing date are excluded: indices carry no limit price and would otherwise
be counted as names that failed to reach one.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Json

from .sentiment_cycle import MAX_LADDER_LOOKBACK, sentiment_reading

#: Identifies the reading, so a later revision of the thresholds is separable
#: from the rows judged under the current ones.
MODEL_VERSION = "sentiment-cycle-v1"


#: Earlier sessions are loaded board-only; the last one is loaded whole.
_BOARD_FILTER = "AND greatest(b.high, b.close) >= b.limit_up - 0.005"
_BARS = """SELECT b.trading_date, b.symbol, b.open, b.high, b.close, b.limit_up
             FROM quant.canonical_bars_daily b
             JOIN quant.instruments i ON i.symbol = b.symbol
            WHERE b.volume > 0 AND b.limit_up IS NOT NULL AND i.list_date IS NOT NULL"""


def _load_sessions(connection: Any, trading_date: date,
                   lookback: int) -> list[tuple[date, list[dict[str, Any]]]]:
    """The last ``lookback`` sessions up to ``trading_date``, oldest first.

    Earlier sessions carry only the names that reached their limit: the ladder,
    the counts and the promotion rate are all about boards, and a sealed name
    is always in that filtered set, so nothing they measure is lost.

    The final session is loaded whole, because the premium reading asks what
    *yesterday's* sealed names did today - including the ones that fell.
    Filtering it to today's boards would average only the names that limited up
    twice and report a premium several times the real one.
    """
    sessions = [row["trading_date"] for row in connection.execute(
        """SELECT DISTINCT trading_date FROM quant.canonical_bars_daily
            WHERE trading_date <= %s AND volume > 0
            ORDER BY trading_date DESC LIMIT %s""",
        (trading_date, max(1, lookback)),
    ).fetchall()]
    if not sessions:
        return []
    latest, earlier = max(sessions), [day for day in sessions if day != max(sessions)]

    grouped: dict[date, list[dict[str, Any]]] = {}
    if earlier:
        for row in connection.execute(
            f"{_BARS} AND b.trading_date = ANY(%s) {_BOARD_FILTER} ORDER BY b.trading_date",
            (earlier,),
        ).fetchall():
            grouped.setdefault(row["trading_date"], []).append(dict(row))
    grouped[latest] = [dict(row) for row in connection.execute(
        f"{_BARS} AND b.trading_date = %s", (latest,)).fetchall()]
    return [(day, grouped[day]) for day in sorted(grouped)]


def materialize_sentiment_cycle(connection: Any, trading_date: date) -> dict[str, Any]:
    """Compute and persist one already-closed session's sentiment reading."""
    sessions = _load_sessions(connection, trading_date, MAX_LADDER_LOOKBACK)
    reading = sentiment_reading(sessions)
    if reading.get("trading_date") != trading_date:
        # The requested date is not a session with any board in it; recording a
        # reading under its name would invent one.
        return {"status": "skipped", "trading_date": str(trading_date),
                "reason": "no limit-up bars for this date"}
    connection.execute(
        """INSERT INTO quant.sentiment_cycle_daily(
               trading_date,model_version,stage,sealed_count,broken_count,broken_rate,
               max_board_height,high_board_count,promotion_rate,prior_limit_up_premium_pct,evidence)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(trading_date) DO UPDATE SET
             model_version=EXCLUDED.model_version,stage=EXCLUDED.stage,
             sealed_count=EXCLUDED.sealed_count,broken_count=EXCLUDED.broken_count,
             broken_rate=EXCLUDED.broken_rate,max_board_height=EXCLUDED.max_board_height,
             high_board_count=EXCLUDED.high_board_count,promotion_rate=EXCLUDED.promotion_rate,
             prior_limit_up_premium_pct=EXCLUDED.prior_limit_up_premium_pct,
             evidence=EXCLUDED.evidence,calculated_at=now()""",
        (trading_date, MODEL_VERSION, reading["stage"], reading["sealed_count"],
         reading["broken_count"], reading["broken_rate"], reading["max_board_height"],
         reading["high_board_count"], reading["promotion_rate"],
         reading["prior_limit_up_premium_pct"],
         Json({**reading, "trading_date": str(trading_date), "model_version": MODEL_VERSION})),
    )
    return {"status": "completed", **reading, "trading_date": str(trading_date),
            "model_version": MODEL_VERSION}


def backfill_sentiment_cycle(connection: Any, trading_dates: list[date]) -> int:
    """Materialize many already-known sessions, for building the study window."""
    return sum(1 for day in trading_dates
               if materialize_sentiment_cycle(connection, day)["status"] == "completed")


def read_sentiment_cycle(connection: Any, trading_date: date) -> dict[str, Any] | None:
    """The stored reading for one session, or None when it was never written."""
    row = connection.execute(
        "SELECT * FROM quant.sentiment_cycle_daily WHERE trading_date=%s",
        (trading_date,),
    ).fetchone()
    return dict(row) if row is not None else None


__all__ = [
    "MODEL_VERSION", "backfill_sentiment_cycle", "materialize_sentiment_cycle",
    "read_sentiment_cycle",
]
